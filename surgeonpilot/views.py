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
    DpdpRetentionRule,
    Doctor,
)
from .serializers import (
    DpdpDeletionRequestSerializer,
    DpdpRetentionRuleSerializer,
    DoctorSerializer,
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

