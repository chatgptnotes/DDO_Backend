"""
Top-level URL routing.

Layout:
- /api/health/                — public liveness check
- /api/me/                    — authenticated user info
- /api/patients/...           — patient registration endpoints
- /api/surgeon/...            — AiSurgeonPilot endpoints
- /api/aidoccall/...          — aidoccall.com endpoints
- /api/payments/...           — Stripe payments (intents + webhook)
"""
from django.urls import include, path

from core.views import HealthView, MeView
from core.patient_views import (
    CurrentPatientProfileView,
    PatientAddressCreateView,
    PatientAllergyCreateView,
    PatientEmergencyContactCreateView,
    PatientInsuranceCreateView,
    PatientMedicalConditionCreateView,
    PatientRegisterView,
)
from core.auth_views import CsrfView, LoginView, LogoutView

urlpatterns = [
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/me/", MeView.as_view(), name="me"),
    path("api/patients/register/", PatientRegisterView.as_view(), name="patient_register"),
    path("api/patients/me/", CurrentPatientProfileView.as_view(), name="patient_me"),
    path("api/patients/me/addresses/", PatientAddressCreateView.as_view(), name="patient_addresses"),
    path("api/patients/me/emergency-contacts/", PatientEmergencyContactCreateView.as_view(), name="patient_emergency_contacts"),
    path("api/patients/me/medical-conditions/", PatientMedicalConditionCreateView.as_view(), name="patient_medical_conditions"),
    path("api/patients/me/allergies/", PatientAllergyCreateView.as_view(), name="patient_allergies"),
    path("api/patients/me/insurance/", PatientInsuranceCreateView.as_view(), name="patient_insurance"),
    path("api/auth/csrf/", CsrfView.as_view(), name="csrf"),
    path("api/auth/login/", LoginView.as_view(), name="login"),
    path("api/auth/logout/", LogoutView.as_view(), name="logout"),
    path("api/surgeon/", include("surgeonpilot.urls")),
    path("api/aidoccall/", include("aidoccall.urls")),
    path("api/payments/", include("payments.urls")),
]
