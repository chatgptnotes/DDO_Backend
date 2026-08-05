"""URL routes for AiSurgeonPilot endpoints. Add resources here as they migrate from Supabase-direct calls."""
from django.urls import path

from .views import (
    AdamritSyncJobStatusView,
    AdamritSyncJobView,
    ClinicDoctorListView,
    TranscribeView,
)

urlpatterns = [
    path("clinic/doctors/", ClinicDoctorListView.as_view(), name="clinic-doctors-list"),
    path("transcribe/", TranscribeView.as_view(), name="transcribe"),
    path("adamrit-sync/", AdamritSyncJobView.as_view(), name="adamrit-sync-job"),
    path("adamrit-sync/<uuid:job_id>/", AdamritSyncJobStatusView.as_view(), name="adamrit-sync-job-status"),
]
