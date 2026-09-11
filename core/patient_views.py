"""
Patient registration endpoints backed by local PostgreSQL.
"""
import re
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import DocPatient, UserRole
from .serializers import PatientRegistrationSerializer
from core.otp_service import consume_challenge, normalize_phone, verify_challenge

OTP_VERIFY_HTTP = {
    "not_found": 404,
    "invalid": 400,
    "expired": 400,
    "used": 400,
    "locked": 429,
}

OTP_VERIFY_MESSAGES = {
    "not_found": "Verification request not found or expired. Please request a new code.",
    "invalid": "Incorrect code. Please try again.",
    "expired": "This code has expired. Please request a new one.",
    "used": "This code was already used. Please request a new one.",
    "locked": "Too many incorrect attempts. Please request a new code.",
}


def _patient_id_for(request) -> str | None:
    """doc_patients.id of the signed-in patient (session-scoped)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM public.doc_patients WHERE user_id = %s LIMIT 1",
            [str(request.user.id)],
        )
        row = cursor.fetchone()
    return str(row[0]) if row else None


def _row_to_dict(cursor) -> dict | None:
    columns = [c[0] for c in cursor.description]
    row = cursor.fetchone()
    return dict(zip(columns, row)) if row else None


def _dump_error(label: str, tb_text: str) -> None:
    """TEMP DEBUG — write the full traceback of a failed patient-records
    request to a log file so it can be inspected after the fact."""
    try:
        from pathlib import Path
        log_path = Path(settings.BASE_DIR) / "patient_records_error.log"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("\n=== " + str(label) + " ===\n" + str(tb_text) + "\n")
    except Exception:
        pass


def _link_patient_profile(user, data) -> bool:
    """Create (or attach) the doc_patients profile for a signed-in account.

    Mirrors the attach branch of PatientRegistrationSerializer.create, but
    for an account that already exists in auth.users/public.users without a
    portal profile. Returns True when the account now has a profile; False
    when an existing profile belongs to a different account (don't hijack it).
    """
    name = str(data.get("full_name") or "").strip()
    name_parts = name.split(maxsplit=1)

    with transaction.atomic():
        existing = (
            DocPatient.objects.select_for_update()
            .filter(email__iexact=user.email)
            .order_by("created_at", "id")
            .first()
        )
        if existing is not None:
            if existing.user_id not in (None, user.pk):
                return False
            if existing.user_id is None:
                existing.user_id = user.pk
                existing.save(update_fields=["user_id"])
            return True

        DocPatient.objects.create(
            user_id=user.pk,
            email=user.email,
            first_name=name_parts[0] or None,
            last_name=name_parts[1] if len(name_parts) > 1 else "",
            phone_number=str(data.get("phone") or "").strip() or None,
            registration_step=1,
            registration_completed=False,
            intake_form_completed=False,
            is_indian_resident=bool(data.get("is_indian_resident", True)),
        )
        if not UserRole.objects.filter(user=user, role="patient", is_active=True).exists():
            UserRole.objects.create(user=user, role="patient", is_active=True)
    return True


class CurrentPatientProfileView(APIView):
    """Return / update the canonical PostgreSQL profile for the signed-in patient."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # ``row_to_json`` deliberately returns the live doc_patients shape so
        # existing portal fields remain available while the remaining patient
        # service methods are migrated from Supabase one by one.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT row_to_json(p) FROM public.doc_patients p "
                "WHERE p.user_id = %s LIMIT 1",
                [str(request.user.id)],
            )
            row = cursor.fetchone()

        if row is None:
            return Response(
                {"success": False, "message": "Patient profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(row[0])

    def patch(self, request):
        """Update whitelisted profile / intake-progress fields."""
        patient_id = _patient_id_for(request)
        if patient_id is None:
            return Response(
                {"success": False, "message": "Patient profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        allowed = {
            "first_name", "last_name", "phone_number", "date_of_birth", "gender",
            "blood_group", "height_cm", "weight_kg",
            "registration_step", "registration_completed", "intake_form_completed",
            "international_consent_completed",
        }
        body = request.data or {}
        assignments = []
        params: list = []
        for column in allowed:
            if column in body:
                assignments.append(f"{column} = %s")
                params.append(body[column])
        if not assignments:
            return Response(
                {"success": False, "message": "No updatable fields supplied"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        assignments.append("updated_at = now()")

        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE public.doc_patients SET {', '.join(assignments)} WHERE id = %s",
                [*params, patient_id],
            )
            cursor.execute(
                "SELECT row_to_json(p) FROM public.doc_patients p WHERE p.id = %s LIMIT 1",
                [patient_id],
            )
            row = cursor.fetchone()

        return Response(row[0])


class PatientRegisterView(APIView):
    """
    POST /api/patients/register/ - Create a new patient in local PostgreSQL.

    Creates a patient account and canonical portal profile in local PostgreSQL.

    If the caller already has a session (an account that exists in
    auth.users/public.users but never got its doc_patients profile — the
    patient portal redirects those users here), the profile is linked to the
    existing account instead of failing with "email already exists".

    Request body:
    {
        "email": "patient@example.com",
        "password": "password123",
        "full_name": "John Doe",
        "phone": "+91 9876543210",
        "date_of_birth": "1990-01-01",
        "gender": "male",
        "address": "123 Main St",
        "city": "Mumbai",
        "state": "Maharashtra"
    }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        """Register a new patient (requires a verified SMS OTP challenge)."""
        body = request.data or {}
        session_user = request.user if getattr(request.user, "is_authenticated", False) else None

        # OTP gate: registration completes only after the patient verifies the
        # code sent to their mobile. The challenge binds BOTH the phone and
        # the email — the client cannot swap either between request/verify.
        otp_request_id = str(body.get("otp_request_id") or "").strip()
        otp_code = str(body.get("otp_code") or "").strip()
        if not otp_request_id or not otp_code:
            return Response({
                'success': False,
                'message': 'Mobile verification required. Request an OTP, then '
                           'submit it together with your registration details.',
            }, status=status.HTTP_400_BAD_REQUEST)

        # Verified but NOT consumed yet: the code stays retryable until the
        # account actually saves, so a validation error (e.g. a password the
        # similarity validator rejects) does not force a new SMS.
        otp_status, challenge = verify_challenge(
            otp_request_id, "register", otp_code, consume_on_ok=False)
        if otp_status != "ok" or challenge is None:
            return Response({
                'success': False,
                'message': OTP_VERIFY_MESSAGES.get(
                    otp_status, 'Mobile verification failed. Please request a new code.'),
            }, status=OTP_VERIFY_HTTP.get(otp_status, status.HTTP_400_BAD_REQUEST))

        # Destination + identity binding (defense in depth).
        body_phone = normalize_phone(str(body.get("phone") or "")) or ""
        if body_phone != challenge["phone"]:
            return Response({
                'success': False,
                'message': 'This code was issued to a different mobile number.',
            }, status=status.HTTP_400_BAD_REQUEST)
        body_email = str(body.get("email") or "").strip().lower()
        if (challenge.get("email") or "").lower() != body_email:
            return Response({
                'success': False,
                'message': 'This code was issued for a different email address.',
            }, status=status.HTTP_400_BAD_REQUEST)

        # Signed-in patient whose profile is missing: create + link it. Without
        # this branch the portal's "no profile -> /patient/register" redirect
        # dead-ends on the email-taken validation error.
        if (
            session_user is not None
            and str(body.get("email") or "").strip().lower() == str(session_user.email).strip().lower()
            and _link_patient_profile(session_user, body)
        ):
            consume_challenge(otp_request_id)
            return Response({
                'success': True,
                'message': 'Patient profile linked successfully',
                'data': {
                    'id': session_user.id,
                    'email': session_user.email,
                    'full_name': session_user.full_name,
                    'role': session_user.role,
                    'created_at': session_user.created_at.isoformat()
                }
            }, status=status.HTTP_201_CREATED)

        serializer = PatientRegistrationSerializer(data=body)

        if serializer.is_valid():
            user = serializer.save()
            consume_challenge(otp_request_id)  # burn only on success
            return Response({
                'success': True,
                'message': 'Patient registered successfully',
                'data': {
                    'id': user.id,
                    'email': user.email,
                    'full_name': user.full_name,
                    'role': user.role,
                    'created_at': user.created_at.isoformat()
                }
            }, status=status.HTTP_201_CREATED)

        # Provide more specific error messages
        error_messages = []
        for field, errors in serializer.errors.items():
            for error in errors:
                if field == 'email' and 'valid' in str(error).lower():
                    error_messages.append(f"Please enter a valid email address (e.g. yourname@gmail.com)")
                elif field == 'password':
                    error_messages.append(str(error))
                elif field == 'email' and 'exists' in str(error).lower():
                    error_messages.append(f"An account with this email already exists")
                else:
                    error_messages.append(f"{field}: {error}")

        return Response({
            'success': False,
            'message': error_messages[0] if error_messages else 'Registration failed',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)


class PatientSubResourceCreateView(APIView):
    """Base for intake sub-resource inserts (address, contacts, medical...).

    Subclasses provide the table and a column->value map built from the body.
    The patient is always resolved from the Django session - never from the
    request body. GET lists every row the session patient owns, newest first -
    the portal pages migrated off Supabase-direct reads use it.
    """

    permission_classes = [IsAuthenticated]
    table = ""

    def get(self, request):
        patient_id = _patient_id_for(request)
        if patient_id is None:
            return Response([])
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM public.{self.table} WHERE patient_id = %s ORDER BY created_at DESC",
                    [patient_id],
                )
                columns = [c[0] for c in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception:
            import traceback as _tb
            _dump_error("GET " + self.table, _tb.format_exc())
            raise
        return Response(rows)

    def post(self, request):
        patient_id = _patient_id_for(request)
        if patient_id is None:
            return Response(
                {"success": False, "message": "Patient profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        columns: dict = {"patient_id": patient_id, **self.columns_for(request.data or {})}
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO public.{self.table} ({', '.join(columns)})
                    VALUES ({', '.join(['%s'] * len(columns))})
                    RETURNING *
                    """,
                    list(columns.values()),
                )
                row = _row_to_dict(cursor)
        except Exception:
            import traceback as _tb
            _dump_error("POST " + self.table, _tb.format_exc())
            raise

        return Response(row, status=status.HTTP_201_CREATED)


class PatientAddressCreateView(PatientSubResourceCreateView):
    """POST /api/patients/me/addresses/ - insert into doc_patient_addresses."""

    table = "doc_patient_addresses"

    def columns_for(self, body):
        if body.get("isPrimary"):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE public.doc_patient_addresses SET is_primary = false "
                    "WHERE patient_id = %s",
                    [_patient_id_for(self.request)],
                )
        return {
            "address_type": body.get("addressType") or "home",
            "address_line_1": body.get("streetAddress"),
            "address_line_2": body.get("apartmentUnit"),
            "city": body.get("city"),
            "state": body.get("state"),
            "postal_code": body.get("postalCode"),
            "country": body.get("country") or "India",
            "is_primary": body.get("isPrimary") is not False,
        }


class PatientEmergencyContactCreateView(PatientSubResourceCreateView):
    """POST /api/patients/me/emergency-contacts/ - doc_patient_emergency_contacts."""

    table = "doc_patient_emergency_contacts"

    def columns_for(self, body):
        if body.get("isPrimary"):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE public.doc_patient_emergency_contacts SET is_primary = false "
                    "WHERE patient_id = %s",
                    [_patient_id_for(self.request)],
                )
        return {
            "contact_name": body.get("contactName"),
            "relationship": body.get("relationship"),
            "phone_number": body.get("phone"),
            "email": body.get("email"),
            "is_primary": body.get("isPrimary") is not False,
        }


class PatientMedicalConditionCreateView(PatientSubResourceCreateView):
    """POST /api/patients/me/medical-conditions/ - doc_patient_medical_history."""

    table = "doc_patient_medical_history"

    def columns_for(self, body):
        return {
            "condition_name": body.get("conditionName"),
            "condition_type": body.get("conditionType") or "chronic",
            # `or None`: an empty string from the form would crash the date insert
            "diagnosed_date": body.get("diagnosedDate") or None,
            "notes": body.get("notes"),
            "is_current": body.get("isCurrent") is not False,
        }


class PatientAllergyCreateView(PatientSubResourceCreateView):
    """POST /api/patients/me/allergies/ - doc_patient_allergies."""

    table = "doc_patient_allergies"

    def columns_for(self, body):
        return {
            "allergy_name": body.get("allergyName"),
            "allergy_type": body.get("allergyType") or "other",
            "severity": body.get("severity"),
            "reaction_description": body.get("reactionDescription"),
        }


class PatientInsuranceCreateView(PatientSubResourceCreateView):
    """POST /api/patients/me/insurance/ - doc_patient_insurance."""

    table = "doc_patient_insurance"

    def columns_for(self, body):
        if body.get("isPrimary"):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE public.doc_patient_insurance SET is_primary = false "
                    "WHERE patient_id = %s",
                    [_patient_id_for(self.request)],
                )
        return {
            "provider_name": body.get("providerName"),
            "policy_number": body.get("policyNumber"),
            "group_number": body.get("groupNumber"),
            "member_id": body.get("memberId"),
            "coverage_type": body.get("coverageType") or "individual",
            "valid_from": body.get("validFrom"),
            "valid_until": body.get("validUntil"),
            "is_primary": body.get("isPrimary") or False,
            "is_active": True,
        }


class PatientMedicationCreateView(PatientSubResourceCreateView):
    """GET/POST /api/patients/me/medications/ - doc_patient_medications."""

    table = "doc_patient_medications"

    def columns_for(self, body):
        return {
            "medication_name": body.get("medicationName"),
            "dosage": body.get("dosage"),
            "frequency": body.get("frequency"),
            "prescribing_doctor": body.get("prescribedBy"),
            "start_date": body.get("startDate"),
            "end_date": body.get("endDate"),
            "is_current": body.get("isCurrent") is not False,
            "notes": body.get("notes"),
        }


class PatientReportCreateView(PatientSubResourceCreateView):
    """POST /api/patients/me/reports/ - doc_patient_reports.

    The file bytes go straight to the storage bucket from the browser; this
    endpoint only records the row (uploaded_by='patient') so doctor-side local
    reads see the upload.
    """

    table = "doc_patient_reports"

    def columns_for(self, body):
        return {
            "doc_patient_id": _patient_id_for(self.request),
            "file_name": body.get("fileName"),
            "file_url": body.get("fileUrl"),
            "file_type": body.get("fileType") or "medical_report",
            "description": body.get("description"),
            "uploaded_by": "patient",
        }


class PatientReportUploadView(APIView):
    """POST /api/patients/me/reports/upload/ - multipart report file upload.

    Stores the bytes on the local server under media/patient-reports/<pid>/
    and records the doc_patient_reports row (uploaded_by='patient'). Replaces
    the remote-bucket upload whose storage policies local-uuid patients could
    not pass. The doctor dashboard streams the file back through
    /api/doctor/patients/<id>/reports/file/.
    """

    permission_classes = [IsAuthenticated]
    max_bytes = 5 * 1024 * 1024

    def post(self, request):
        patient_id = _patient_id_for(request)
        if patient_id is None:
            return Response(
                {"success": False, "message": "Patient profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response(
                {"success": False, "message": "A report file is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if uploaded.size > self.max_bytes:
            return Response(
                {"success": False, "message": "File is too large. Maximum size is 5MB."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", uploaded.name or "report")
        rel_path = f"patient-reports/{patient_id}/{int(time.time() * 1000)}_{safe_name}"
        abs_path = Path(settings.BASE_DIR) / "media" / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(abs_path, "wb") as destination:
            for chunk in uploaded.chunks():
                destination.write(chunk)

        columns = {
            "patient_id": patient_id,
            "doc_patient_id": patient_id,
            "file_name": uploaded.name or safe_name,
            "file_url": rel_path,
            "file_type": request.data.get("fileType") or "medical_report",
            "description": request.data.get("description") or None,
            "uploaded_by": "patient",
        }
        # Optional bindings from the portal upload flow (booking context)
        doctor_id = request.data.get("doctorId")
        if doctor_id:
            columns["doctor_id"] = doctor_id
        appointment_id = request.data.get("appointmentId")
        if appointment_id:
            columns["appointment_id"] = appointment_id
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO public.doc_patient_reports ({', '.join(columns)})
                VALUES ({', '.join(['%s'] * len(columns))})
                RETURNING *
                """,
                list(columns.values()),
            )
            row = _row_to_dict(cursor)

        return Response(row, status=status.HTTP_201_CREATED)


class PatientSubResourceDeleteView(APIView):
    """DELETE one owned sub-resource row (uuid item id, session patient)."""

    permission_classes = [IsAuthenticated]
    table = ""

    def delete(self, request, item_id):
        patient_id = _patient_id_for(request)
        if patient_id is None:
            return Response(
                {"success": False, "message": "Patient profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            item_uuid = uuid.UUID(str(item_id))
        except ValueError:
            return Response(
                {"success": False, "message": "Invalid id"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM public.{self.table} WHERE id = %s AND patient_id = %s",
                [str(item_uuid), patient_id],
            )
            deleted = cursor.rowcount
        if not deleted:
            return Response({"success": False, "message": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"success": True})


class PatientMedicalConditionDeleteView(PatientSubResourceDeleteView):
    """DELETE /api/patients/me/medical-conditions/<id>/."""

    table = "doc_patient_medical_history"


class PatientAllergyDeleteView(PatientSubResourceDeleteView):
    """DELETE /api/patients/me/allergies/<id>/."""

    table = "doc_patient_allergies"


class PatientMedicationDeleteView(PatientSubResourceDeleteView):
    """DELETE /api/patients/me/medications/<id>/."""

    table = "doc_patient_medications"


class PatientReportFileView(APIView):
    """GET /api/patients/me/reports/file/?path=<relative media path>

    Streams one of the session patient's own uploaded/shared documents from
    the local media store (media/patient-reports/<pid>/...). The path must
    start with the patient's own folder — no traversal, no other patient's
    files. Replaces the retired remote-bucket signed/public URLs.
    """

    permission_classes = [IsAuthenticated]

    content_types = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".html": "text/html",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    def get(self, request):
        from django.http import FileResponse
        import re as _re
        from pathlib import Path as _Path

        patient_id = _patient_id_for(request)
        if patient_id is None:
            return Response(
                {"success": False, "message": "Patient profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        requested = request.query_params.get("path") or ""
        expected_prefix = f"patient-reports/{patient_id}/"
        if not requested.startswith(expected_prefix) or ".." in requested.split("/"):
            return Response(
                {"success": False, "message": "Not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        abs_path = _Path(settings.BASE_DIR) / "media" / requested
        if not abs_path.resolve().is_relative_to((_Path(settings.BASE_DIR) / "media").resolve()):
            return Response(
                {"success": False, "message": "Not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not abs_path.is_file():
            return Response(
                {"success": False, "message": "Not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        ext = abs_path.suffix.lower()
        content_type = self.content_types.get(ext, "application/octet-stream")
        response = FileResponse(open(abs_path, "rb"), content_type=content_type)
        response["Content-Disposition"] = f'inline; filename="{abs_path.name}"'
        return response
