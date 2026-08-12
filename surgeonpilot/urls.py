"""URL routes for AiSurgeonPilot endpoints. Add resources here as they migrate from Supabase-direct calls."""
from django.urls import path

from .views import (
    AdamritSyncJobStatusView,
    AdamritSyncJobView,
    CancelDeletionRequestView,
    ClinicDoctorListView,
    CreateManualDeletionRequestView,
    DpdpDeletionRequestDetailView,
    DpdpDeletionRequestListView,
    DpdpRetentionRuleListView,
    TranscribeView,
)

urlpatterns = [
    path("clinic/doctors/", ClinicDoctorListView.as_view(), name="clinic-doctors-list"),
    path("transcribe/", TranscribeView.as_view(), name="transcribe"),
    path("adamrit-sync/", AdamritSyncJobView.as_view(), name="adamrit-sync-job"),
    path("adamrit-sync/<uuid:job_id>/", AdamritSyncJobStatusView.as_view(), name="adamrit-sync-job-status"),
    # DPDP Data Deletion (Admin Only)
    path("dpdp/retention-rules/", DpdpRetentionRuleListView.as_view(), name="dpdp-retention-rules"),
    path("dpdp/deletion-requests/", DpdpDeletionRequestListView.as_view(), name="dpdp-deletion-requests-list"),
    path("dpdp/deletion-requests/create/", CreateManualDeletionRequestView.as_view(), name="dpdp-deletion-request-create"),
    path("dpdp/deletion-requests/<uuid:request_id>/", DpdpDeletionRequestDetailView.as_view(), name="dpdp-deletion-request-detail"),
    path("dpdp/deletion-requests/<uuid:request_id>/cancel/", CancelDeletionRequestView.as_view(), name="dpdp-deletion-request-cancel"),
]
