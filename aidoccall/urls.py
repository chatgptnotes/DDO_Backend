"""URL routes for aidoccall.com endpoints. Add resources here as they migrate from Supabase-direct calls."""
from django.urls import path

from .views import CreateDoctorView, PatientSelectedDoctorsView

urlpatterns = [
    path(
        "patient/selected-doctors/",
        PatientSelectedDoctorsView.as_view(),
        name="patient-selected-doctors",
    ),
    path(
        "admin/doctors/",
        CreateDoctorView.as_view(),
        name="admin-create-doctor",
    ),
]
