"""URL routes for AiSurgeonPilot endpoints. Add resources here as they migrate from Supabase-direct calls."""
from django.urls import path

from .views import ClinicDoctorListView, TranscribeView

urlpatterns = [
    path("clinic/doctors/", ClinicDoctorListView.as_view(), name="clinic-doctors-list"),
    path("transcribe/", TranscribeView.as_view(), name="transcribe"),
]
