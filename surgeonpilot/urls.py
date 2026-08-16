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
    PatientConsentAuditView,
    PatientConsentStatusView,
    PatientDeletionAuditView,
    PatientDeletionDetailView,
    PatientDeletionHistoryView,
    PatientDeletionRequestView,
    ProcessingPurposeListView,
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
    # Patient Consent Management
    path("processing-purposes/", ProcessingPurposeListView.as_view(), name="processing-purposes"),
    path("patient-consent/<uuid:patient_id>/", PatientConsentStatusView.as_view(), name="patient-consent-status"),
    path("patient-consent/<uuid:patient_id>/audit/", PatientConsentAuditView.as_view(), name="patient-consent-audit"),
    # Patient Data Deletion
    path("patient/deletion-request/", PatientDeletionRequestView.as_view(), name="patient-deletion-request"),
    path("patient/deletion-history/", PatientDeletionHistoryView.as_view(), name="patient-deletion-history"),
    path("patient/deletion-request/<uuid:request_id>/", PatientDeletionDetailView.as_view(), name="patient-deletion-detail"),
    path("patient/deletion-request/<uuid:request_id>/audit/", PatientDeletionAuditView.as_view(), name="patient-deletion-audit"),
]
