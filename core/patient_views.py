"""
Patient registration endpoints backed by local PostgreSQL.
"""
from django.db import connection, transaction
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import DocPatient, UserRole
from .serializers import PatientRegistrationSerializer


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
        """Register a new patient."""
        body = request.data or {}
        session_user = request.user if getattr(request.user, "is_authenticated", False) else None

        # Signed-in patient whose profile is missing: create + link it. Without
        # this branch the portal's "no profile -> /patient/register" redirect
        # dead-ends on the email-taken validation error.
        if (
            session_user is not None
            and str(body.get("email") or "").strip().lower() == str(session_user.email).strip().lower()
            and _link_patient_profile(session_user, body)
        ):
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
    request body.
    """

    permission_classes = [IsAuthenticated]
    table = ""

    def post(self, request):
        patient_id = _patient_id_for(request)
        if patient_id is None:
            return Response(
                {"success": False, "message": "Patient profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        columns: dict = {"patient_id": patient_id, **self.columns_for(request.data or {})}
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
            "diagnosed_date": body.get("diagnosedDate"),
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
