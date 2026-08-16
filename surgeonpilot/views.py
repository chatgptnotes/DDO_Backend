"""
Views for AiSurgeonPilot endpoints migrated from direct Supabase calls.

Each view should return JSON in the exact same shape PostgREST returned, so
the frontend can be swapped over behind a feature flag without code changes
beyond the call site.
"""
from __future__ import annotations

import uuid

from django.db import IntegrityError, connection
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRole

from .models import (
    AdamritSyncJob,
    DpdpDeletionRequest,
    DpdpProcessingPurpose,
    DpdpRetentionRule,
    Doctor,
    PatientConsentPreference,
)
from .serializers import (
    DpdpDeletionRequestSerializer,
    DpdpProcessingPurposeSerializer,
    DpdpRetentionRuleSerializer,
    DoctorSerializer,
    PatientConsentPreferenceSerializer,
)
from .services.transcription import (
    DEFAULT_LANGUAGE,
    TranscriptionError,
    transcribe_audio,
)

# Reject uploads larger than this outright (a long consultation is still only a
# few MB of compressed opus; anything bigger is almost certainly a mistake).
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class ClinicDoctorListView(ListAPIView):
    """Replaces the Supabase call on the Branding page:

        supabase.from('doc_doctors')
                .select('*')
                .eq('role', 'doctor')
                .eq('created_by', currentUser.id)
                .order('full_name')

    Authorization: requester must hold `clinical_admin` or `superadmin`.
    Filtering: only doctors *they* created (`created_by = request.user.id`).
    """

    serializer_class = DoctorSerializer
    permission_classes = [IsAuthenticated, HasRole("clinical_admin", "superadmin")]
    pagination_class = None  # plain array, mirroring PostgREST default response shape

    def get_queryset(self):
        return (
            Doctor.objects.filter(role="doctor", created_by=self.request.user.id)
            .order_by("full_name")
        )

    def list(self, request, *args, **kwargs):
        # Override to guarantee a plain array — DRF's default already does this
        # when pagination_class is None, but be explicit so the response shape
        # is locked down even if a developer adds pagination later.
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class TranscribeView(APIView):
    """Transcribe a recorded in-person consultation to text.

        POST /api/surgeon/transcribe/    (multipart/form-data)
            audio:    the recorded audio file (required)
            language: BCP-47 tag, default 'hi-IN'

    Returns `{ "transcript", "language", "engine" }`. An empty transcript means
    no speech was recognised (still 200). Engine failures (network/decoding)
    return 502. Only doctors (or clinical admins / superadmins acting in the
    portal) may transcribe.
    """

    parser_classes = [MultiPartParser, FormParser]
    # Gated on authentication only: this is a stateless speech-to-text utility
    # that returns only the caller's own uploaded audio as text (no patient data
    # is read or written) and is reachable only from inside the doctor portal.
    # NOTE: to restrict to clinicians, add back
    # `HasRole("doctor", "clinical_admin", "superadmin")` once the relevant
    # accounts hold an active row in the `user_roles` table.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        audio_file = request.FILES.get("audio")
        if audio_file is None:
            return Response(
                {"detail": "No audio file provided (expected field 'audio')."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if audio_file.size == 0:
            return Response(
                {"detail": "The audio file is empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if audio_file.size > MAX_AUDIO_BYTES:
            return Response(
                {"detail": "Audio file is too large (max 25 MB)."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        language = (request.data.get("language") or DEFAULT_LANGUAGE).strip()

        try:
            result = transcribe_audio(
                audio_bytes=audio_file.read(),
                filename=audio_file.name or "",
                language=language,
            )
        except TranscriptionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "transcript": result.transcript,
                "language": result.language,
                "engine": result.engine,
            }
        )


MURALI_SYNC_EMAIL = "cmd@hopehospital.com"


def _can_manage_adamrit_sync(request) -> bool:
    return (request.user.email or "").strip().lower() == MURALI_SYNC_EMAIL


def _job_payload(job: AdamritSyncJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "result_summary": job.result_summary,
        "error_message": job.error_message if job.status == "failed" else None,
    }


class AdamritSyncJobView(APIView):
    """Queue the one-way Adamrit sync for the dedicated Dr. Murali account."""

    permission_classes = [IsAuthenticated, HasRole("doctor")]

    def post(self, request):
        if not _can_manage_adamrit_sync(request):
            return Response(
                {"detail": "This sync is not available for this account."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            job = AdamritSyncJob.objects.create(
                id=uuid.uuid4(),
                requested_by_id=request.user.id,
                status="queued",
            )
        except IntegrityError:
            active_job = (
                AdamritSyncJob.objects.filter(status__in=["queued", "running"])
                .order_by("created_at")
                .first()
            )
            if active_job:
                return Response(_job_payload(active_job), status=status.HTTP_202_ACCEPTED)
            raise

        return Response(_job_payload(job), status=status.HTTP_202_ACCEPTED)


class AdamritSyncJobStatusView(APIView):
    permission_classes = [IsAuthenticated, HasRole("doctor")]

    def get(self, request, job_id):
        if not _can_manage_adamrit_sync(request):
            return Response(
                {"detail": "This sync is not available for this account."},
                status=status.HTTP_403_FORBIDDEN,
            )

        job = AdamritSyncJob.objects.filter(
            id=job_id, requested_by_id=request.user.id
        ).first()
        if not job:
            return Response(
                {"detail": "Sync job not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(_job_payload(job))


# =====================================================
# DPDP Data Deletion Views (Admin Only)
# =====================================================


class DpdpRetentionRuleListView(ListAPIView):
    """List all retention rules. Superadmin only."""

    serializer_class = DpdpRetentionRuleSerializer
    permission_classes = [IsAuthenticated, HasRole("superadmin")]
    pagination_class = None

    def get_queryset(self):
        return DpdpRetentionRule.objects.filter(is_active=True).order_by("priority")


class DpdpDeletionRequestListView(ListAPIView):
    """List all deletion requests. Superadmin only."""

    serializer_class = DpdpDeletionRequestSerializer
    permission_classes = [IsAuthenticated, HasRole("superadmin")]

    def get_queryset(self):
        return DpdpDeletionRequest.objects.all().order_by("-created_at")


class CreateManualDeletionRequestView(APIView):
    """Manually create a deletion request for a patient. Superadmin only.

    POST /api/surgeon/dpdp/deletion-requests/
        {
            "patient_id": "uuid",
            "reason": "Optional reason for manual deletion"
        }
    """

    permission_classes = [IsAuthenticated, HasRole("superadmin")]

    def post(self, request):
        patient_id = request.data.get("patient_id")
        reason = request.data.get("reason", "Admin manual deletion")

        if not patient_id:
            return Response(
                {"detail": "patient_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            patient_id_uuid = uuid.UUID(patient_id)
        except ValueError:
            return Response(
                {"detail": "Invalid patient_id format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if patient exists
        with connection.cursor() as cur:
            cur.execute(
                "SELECT id, email FROM doc_patients WHERE id = %s",
                [patient_id_uuid],
            )
            patient = cur.fetchone()

            if not patient:
                return Response(
                    {"detail": "Patient not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            patient_email = patient[1]

        # Check for pending deletion request
        existing = DpdpDeletionRequest.objects.filter(
            patient_id=patient_id_uuid,
            status__in=["pending", "in_progress"],
        ).first()

        if existing:
            serializer = DpdpDeletionRequestSerializer(existing)
            return Response(
                {
                    "detail": "Pending deletion request already exists.",
                    "request": serializer.data,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Create deletion request
        deletion_request = DpdpDeletionRequest.objects.create(
            id=uuid.uuid4(),
            patient_id=patient_id_uuid,
            patient_email=patient_email,
            request_type="admin_manual",
            reason=reason,
            status="pending",
            created_by=request.user.id,
        )

        serializer = DpdpDeletionRequestSerializer(deletion_request)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class DpdpDeletionRequestDetailView(APIView):
    """Get details of a specific deletion request. Superadmin only."""

    permission_classes = [IsAuthenticated, HasRole("superadmin")]

    def get(self, request, request_id):
        try:
            deletion_request = DpdpDeletionRequest.objects.get(id=request_id)
        except DpdpDeletionRequest.DoesNotExist:
            return Response(
                {"detail": "Deletion request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = DpdpDeletionRequestSerializer(deletion_request)
        return Response(serializer.data)


class CancelDeletionRequestView(APIView):
    """Cancel a pending deletion request. Superadmin only.

    DELETE /api/surgeon/dpdp/deletion-requests/<request_id>/cancel
    """

    permission_classes = [IsAuthenticated, HasRole("superadmin")]

    def delete(self, request, request_id):
        try:
            deletion_request = DpdpDeletionRequest.objects.get(id=request_id)
        except DpdpDeletionRequest.DoesNotExist:
            return Response(
                {"detail": "Deletion request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if deletion_request.status not in ["pending", "in_progress"]:
            return Response(
                {"detail": "Cannot cancel a completed or failed request."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deletion_request.status = "cancelled"
        deletion_request.completed_at = timezone.now()
        deletion_request.save(update_fields=["status", "completed_at"])

        serializer = DpdpDeletionRequestSerializer(deletion_request)
        return Response(serializer.data)


# =====================================================
# Patient Consent Management Views
# =====================================================


class ProcessingPurposeListView(ListAPIView):
    """List all processing purposes with patient consent status.

    GET /api/surgeon/processing-purposes/
    GET /api/surgeon/processing-purposes/?patient_id=<uuid>
    """

    serializer_class = DpdpProcessingPurposeSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return DpdpProcessingPurpose.objects.all().order_by("-is_mandatory", "purpose_name")

    def list(self, request, *args, **kwargs):
        """Return purposes with current patient consent status if patient_id provided."""
        queryset = self.filter_queryset(self.get_queryset())
        patient_id = request.query_params.get("patient_id")

        # First, get all purposes
        purposes = DpdpProcessingPurposeSerializer(queryset, many=True).data

        # If patient_id provided, fetch their consent preferences
        consent_map = {}
        if patient_id:
            try:
                patient_id_uuid = uuid.UUID(patient_id)
                preferences = PatientConsentPreference.objects.filter(
                    patient_id=patient_id_uuid
                )
                for pref in preferences:
                    consent_map[pref.purpose_code] = {
                        "consent_granted": pref.consent_granted,
                        "granted_at": pref.granted_at.isoformat() if pref.granted_at else None,
                        "revoked_at": pref.revoked_at.isoformat() if pref.revoked_at else None,
                        "last_updated_at": pref.last_updated_at.isoformat() if pref.last_updated_at else None,
                        "consent_source": pref.consent_source,
                    }
            except ValueError:
                pass  # Invalid UUID, continue without consent data

        # Attach consent status to each purpose
        for purpose in purposes:
            purpose_code = purpose["purpose_code"]
            if purpose_code in consent_map:
                purpose["consent"] = consent_map[purpose_code]
            elif not purpose["is_mandatory"]:
                # Optional purpose with no consent recorded = not consented
                purpose["consent"] = {"consent_granted": False}
            # Mandatory purposes are always considered consented (service contract)

        return Response(purposes)


class PatientConsentStatusView(APIView):
    """Get or update patient consent preferences.

    GET /api/surgeon/patient-consent/<patient_id>/
    POST /api/surgeon/patient-consent/<patient_id>/
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, patient_id):
        """Get patient's current consent status for all purposes."""
        try:
            patient_id_uuid = uuid.UUID(patient_id)
        except ValueError:
            return Response(
                {"detail": "Invalid patient_id format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Use the database function to get consent status
        with connection.cursor() as cur:
            cur.execute(
                "SELECT * FROM get_patient_consent_status(%s)",
                [patient_id_uuid],
            )
            columns = [desc[0] for desc in cur.description]
            results = []
            for row in cur.fetchall():
                results.append(dict(zip(columns, row)))

        return Response(results)

    def post(self, request, patient_id):
        """Update patient consent for specific purposes.

        POST body: {
          "consents": [
            {"purpose_code": "DATA_EXPORT", "consent_granted": true},
            {"purpose_code": "ABDM_HI_ACCESS", "consent_granted": false}
          ],
          "tenant_id": "<doctor_id>"  // optional
        }
        """
        try:
            patient_id_uuid = uuid.UUID(patient_id)
        except ValueError:
            return Response(
                {"detail": "Invalid patient_id format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        consents = request.data.get("consents", [])
        tenant_id = request.data.get("tenant_id")

        if not isinstance(consents, list):
            return Response(
                {"detail": "consents must be an array"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        results = []
        errors = []

        for consent_item in consents:
            purpose_code = consent_item.get("purpose_code")
            consent_granted = consent_item.get("consent_granted")

            if not purpose_code or consent_granted is None:
                errors.append({
                    "purpose_code": purpose_code,
                    "error": "Missing purpose_code or consent_granted"
                })
                continue

            # Check if purpose is mandatory (cannot be revoked)
            try:
                purpose = DpdpProcessingPurpose.objects.get(purpose_code=purpose_code)
                if purpose.is_mandatory and not consent_granted:
                    errors.append({
                        "purpose_code": purpose_code,
                        "error": "Cannot revoke mandatory processing purpose"
                    })
                    continue
            except DpdpProcessingPurpose.DoesNotExist:
                errors.append({
                    "purpose_code": purpose_code,
                    "error": "Processing purpose not found"
                })
                continue

            # Use the database function to set consent
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT set_patient_consent(%s, %s, %s, 'manual', %s)",
                    [
                        patient_id_uuid,
                        purpose_code,
                        consent_granted,
                        tenant_id,
                    ],
                )
                result = cur.fetchone()[0]

            results.append({
                "purpose_code": purpose_code,
                "consent_granted": consent_granted,
                "success": result.get("success", False),
            })

        return Response({
            "updated": results,
            "errors": errors,
        })


class PatientConsentAuditView(APIView):
    """Get audit trail of patient consent changes.

    GET /api/surgeon/patient-consent/<patient_id>/audit
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, patient_id):
        """Get audit history of consent changes for a patient."""
        try:
            patient_id_uuid = uuid.UUID(patient_id)
        except ValueError:
            return Response(
                {"detail": "Invalid patient_id format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        preferences = PatientConsentPreference.objects.filter(
            patient_id=patient_id_uuid
        ).order_by("-last_updated_at")

        serializer = PatientConsentPreferenceSerializer(preferences, many=True)
        return Response(serializer.data)


# =====================================================
# Patient Data Deletion Views (Patient Portal)
# =====================================================


class PatientDeletionRequestView(APIView):
    """Create a deletion request for the authenticated patient's own data.

    POST /api/surgeon/patient/deletion-request
        {
            "reason": "Optional reason for deletion request"
        }
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Create a deletion request for the authenticated patient."""
        patient_id = request.data.get("patient_id")
        reason = request.data.get("reason", "")

        # If patient_id not provided, try to find patient record from user
        if not patient_id:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT id FROM doc_patients WHERE user_id = %s",
                    [request.user.id],
                )
                patient = cur.fetchone()
                if not patient:
                    return Response(
                        {"detail": "Patient record not found for this user."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                patient_id = str(patient[0])

        try:
            patient_id_uuid = uuid.UUID(patient_id)
        except ValueError:
            return Response(
                {"detail": "Invalid patient_id format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Verify the patient belongs to the requesting user
        with connection.cursor() as cur:
            cur.execute(
                "SELECT id, email FROM doc_patients WHERE id = %s AND user_id = %s",
                [patient_id_uuid, request.user.id],
            )
            patient = cur.fetchone()

            if not patient:
                return Response(
                    {"detail": "Patient not found or access denied."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            patient_email = patient[1]

        # Check for pending deletion request
        existing = DpdpDeletionRequest.objects.filter(
            patient_id=patient_id_uuid,
            status__in=["pending", "in_progress"],
        ).first()

        if existing:
            serializer = DpdpDeletionRequestSerializer(existing)
            return Response(
                {
                    "detail": "Pending deletion request already exists.",
                    "request": serializer.data,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Generate reference number
        import random
        import string
        ref_chars = string.ascii_uppercase + string.digits
        reference_number = f"DEL-{ ''.join(random.choices(ref_chars, k=8)) }"

        # Create deletion request
        deletion_request = DpdpDeletionRequest.objects.create(
            id=uuid.uuid4(),
            reference_number=reference_number,
            patient_id=patient_id_uuid,
            patient_email=patient_email,
            request_type="patient_request",
            reason=reason,
            status="pending",
            created_by=request.user.id,
        )

        serializer = DpdpDeletionRequestSerializer(deletion_request)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class PatientDeletionHistoryView(APIView):
    """Get deletion request history for the authenticated patient.

    GET /api/surgeon/patient/deletion-history
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get all deletion requests for the authenticated patient."""
        # Find patient record for this user
        with connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM doc_patients WHERE user_id = %s",
                [request.user.id],
            )
            patient = cur.fetchone()

            if not patient:
                return Response(
                    {"detail": "Patient record not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            patient_id = patient[0]

        # Get all deletion requests for this patient
        deletion_requests = DpdpDeletionRequest.objects.filter(
            patient_id=patient_id
        ).order_by("-created_at")

        serializer = DpdpDeletionRequestSerializer(deletion_requests, many=True)
        return Response(serializer.data)


class PatientDeletionDetailView(APIView):
    """Get details of a specific deletion request for the authenticated patient.

    GET /api/surgeon/patient/deletion-request/<request_id>
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, request_id):
        """Get details of a specific deletion request."""
        try:
            request_uuid = uuid.UUID(request_id)
        except ValueError:
            return Response(
                {"detail": "Invalid request_id format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Find patient record for this user
        with connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM doc_patients WHERE user_id = %s",
                [request.user.id],
            )
            patient = cur.fetchone()

            if not patient:
                return Response(
                    {"detail": "Patient record not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            patient_id = patient[0]

        # Get the deletion request
        try:
            deletion_request = DpdpDeletionRequest.objects.get(
                id=request_uuid,
                patient_id=patient_id
            )
        except DpdpDeletionRequest.DoesNotExist:
            return Response(
                {"detail": "Deletion request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = DpdpDeletionRequestSerializer(deletion_request)
        return Response(serializer.data)


class PatientDeletionAuditView(APIView):
    """Get audit trail for a patient's deletion request.

    GET /api/surgeon/patient/deletion-request/<request_id>/audit
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, request_id):
        """Get audit trail for a deletion request."""
        try:
            request_uuid = uuid.UUID(request_id)
        except ValueError:
            return Response(
                {"detail": "Invalid request_id format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Find patient record for this user
        with connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM doc_patients WHERE user_id = %s",
                [request.user.id],
            )
            patient = cur.fetchone()

            if not patient:
                return Response(
                    {"detail": "Patient record not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            patient_id = patient[0]

        # Verify the deletion request belongs to this patient
        try:
            deletion_request = DpdpDeletionRequest.objects.get(
                id=request_uuid,
                patient_id=patient_id
            )
        except DpdpDeletionRequest.DoesNotExist:
            return Response(
                {"detail": "Deletion request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Get audit records for this deletion request
        audit_records = DpdpDeletionAudit.objects.filter(
            deletion_request_id=request_uuid
        ).order_by("execution_time")

        return Response([
            {
                "id": str(record.id),
                "table_name": record.table_name,
                "record_id": str(record.record_id) if record.record_id else None,
                "record_type": record.record_type,
                "deleted_fields": record.deleted_fields,
                "record_summary": record.record_summary,
                "execution_time": record.execution_time.isoformat() if record.execution_time else None,
                "executed_by": record.executed_by,
                "status": record.status,
                "error_details": record.error_details,
            }
            for record in audit_records
        ])

