"""URL routes for aidoccall.com endpoints. Add resources here as they migrate from Supabase-direct calls."""
from django.urls import path

from .appointment_views import (
    AppointmentCancelView,
    AppointmentConfirmPaymentView,
    AppointmentListCreateView,
    AppointmentReceiptView,
    AppointmentRescheduleRequestView,
    DoctorAvailabilityScheduleView,
    DoctorAvailabilityView,
)
from .views import CreateDoctorView, DoctorDirectoryView, PatientSelectedDoctorsView

urlpatterns = [
    path(
        "doctors/",
        DoctorDirectoryView.as_view(),
        name="doctor-directory",
    ),
    path(
        "doctors/<uuid:doctor_id>/availability/",
        DoctorAvailabilityView.as_view(),
        name="doctor-availability",
    ),
    path(
        "doctors/<uuid:doctor_id>/availability-schedule/",
        DoctorAvailabilityScheduleView.as_view(),
        name="doctor-availability-schedule",
    ),
    path(
        "appointments/",
        AppointmentListCreateView.as_view(),
        name="patient-appointments",
    ),
    path(
        "appointments/<uuid:appointment_id>/confirm-payment/",
        AppointmentConfirmPaymentView.as_view(),
        name="appointment-confirm-payment",
    ),
    path(
        "appointments/<uuid:appointment_id>/receipt/",
        AppointmentReceiptView.as_view(),
        name="appointment-receipt",
    ),
    path(
        "appointments/<uuid:appointment_id>/cancel/",
        AppointmentCancelView.as_view(),
        name="appointment-cancel",
    ),
    path(
        "appointments/<uuid:appointment_id>/reschedule-request/",
        AppointmentRescheduleRequestView.as_view(),
        name="appointment-reschedule-request",
    ),
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
