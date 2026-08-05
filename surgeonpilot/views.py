"""
Views for AiSurgeonPilot endpoints migrated from direct Supabase calls.

Each view should return JSON in the exact same shape PostgREST returned, so
the frontend can be swapped over behind a feature flag without code changes
beyond the call site.
"""
from __future__ import annotations

import uuid

from django.db import IntegrityError
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRole

from .models import AdamritSyncJob, Doctor
from .serializers import DoctorSerializer


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
            return Response({"detail": "This sync is not available for this account."}, status=status.HTTP_403_FORBIDDEN)

        try:
            job = AdamritSyncJob.objects.create(
                id=uuid.uuid4(),
                requested_by_id=request.user.id,
                status="queued",
            )
        except IntegrityError:
            active_job = AdamritSyncJob.objects.filter(status__in=["queued", "running"]).order_by("created_at").first()
            if active_job:
                return Response(_job_payload(active_job), status=status.HTTP_202_ACCEPTED)
            raise

        return Response(_job_payload(job), status=status.HTTP_202_ACCEPTED)


class AdamritSyncJobStatusView(APIView):
    permission_classes = [IsAuthenticated, HasRole("doctor")]

    def get(self, request, job_id):
        if not _can_manage_adamrit_sync(request):
            return Response({"detail": "This sync is not available for this account."}, status=status.HTTP_403_FORBIDDEN)

        job = AdamritSyncJob.objects.filter(id=job_id, requested_by_id=request.user.id).first()
        if not job:
            return Response({"detail": "Sync job not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_job_payload(job))
