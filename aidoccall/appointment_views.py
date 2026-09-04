"""Patient availability + appointment endpoints backed by local PostgreSQL.

Replaces the Supabase-direct booking calls in
`aidoccall.com/src/services/patientService.js`:

    getDoctorAvailabilitySchedule  -> GET  /api/aidoccall/doctors/<id>/availability-schedule/
    getDoctorAvailability          -> GET  /api/aidoccall/doctors/<id>/availability/?date=
    createAppointment              -> POST /api/aidoccall/appointments/
    getAppointments / getUpcomingAppointments -> GET /api/aidoccall/appointments/
    confirmPayment                 -> PATCH /api/aidoccall/appointments/<id>/confirm-payment/
    cancelAppointment              -> PATCH /api/aidoccall/appointments/<id>/cancel/
    requestReschedule              -> PATCH /api/aidoccall/appointments/<id>/reschedule-request/

The booking patient is always resolved from the Django session
(`doc_patients.user_id = request.user.id`) and every mutation is scoped to
that patient - never from the request body.

Slot GENERATION (expanding availability windows into bookable slots) stays
client-side; these endpoints return the raw pieces (override, availability
windows, booked start times).
"""
from __future__ import annotations

import datetime
import logging
import uuid

from django.db import connection
from django.http import HttpResponse
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .services.receipts import build_receipt_pdf, fetch_receipt_data
from .views import _resolve_patient_id_for_user

logger = logging.getLogger(__name__)


def _require_uuid(value: str):
    """Parse a uuid string; raises ValueError on malformed input."""
    return uuid.UUID(str(value))


def _require_date(value: str) -> datetime.date:
    return datetime.date.fromisoformat(str(value))


def _dicts(cur) -> list[dict]:
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _dict(cur) -> dict | None:
    rows = _dicts(cur)
    return rows[0] if rows else None


def _patient_id_or_response(request) -> tuple[str | None, Response | None]:
    patient_id = _resolve_patient_id_for_user(request.user.id)
    if not patient_id:
        return None, Response(
            {"detail": "No patient profile is linked to this account."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return patient_id, None


class DoctorAvailabilityView(APIView):
    """GET /api/aidoccall/doctors/<doctor_id>/availability/?date=YYYY-MM-DD

    Returns the raw pieces the client needs to generate bookable slots:
    {override, availability, booked_times}.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, doctor_id):
        try:
            _require_uuid(doctor_id)
        except ValueError:
            return Response({"detail": "Invalid doctor id."}, status=status.HTTP_400_BAD_REQUEST)

        date_str = (request.query_params.get("date") or "").strip()
        try:
            date_obj = _require_date(date_str)
        except ValueError:
            return Response(
                {"detail": "A `date` query parameter in YYYY-MM-DD format is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # doc_availability uses 0=Sunday..6=Saturday (matching JS getDay()).
        day_of_week = (date_obj.weekday() + 1) % 7

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT date, is_available
                  FROM doc_availability_overrides
                 WHERE doctor_id = %s AND date = %s
                 LIMIT 1
                """,
                [doctor_id, date_obj],
            )
            override = _dict(cur)

            cur.execute(
                """
                SELECT id, start_time, end_time, slot_duration, visit_type
                  FROM doc_availability
                 WHERE doctor_id = %s
                   AND day_of_week = %s
                   AND COALESCE(is_active, true) = true
                 ORDER BY start_time
                """,
                [doctor_id, day_of_week],
            )
            availability = _dicts(cur)

            cur.execute(
                """
                SELECT start_time
                  FROM doc_appointments
                 WHERE doctor_id = %s
                   AND appointment_date = %s
                   AND status IN ('pending', 'confirmed')
                """,
                [doctor_id, date_obj],
            )
            booked_times = [r[0] for r in cur.fetchall()]

        return Response(
            {
                "override": override,
                "availability": availability,
                "booked_times": booked_times,
            }
        )


class DoctorAvailabilityScheduleView(APIView):
    """GET /api/aidoccall/doctors/<doctor_id>/availability-schedule/

    Returns {working_days: [0-6], overrides: {'YYYY-MM-DD': bool}} so the
    patient calendar can disable unavailable dates up front.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, doctor_id):
        try:
            _require_uuid(doctor_id)
        except ValueError:
            return Response({"detail": "Invalid doctor id."}, status=status.HTTP_400_BAD_REQUEST)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT day_of_week
                  FROM doc_availability
                 WHERE doctor_id = %s
                   AND COALESCE(is_active, true) = true
                """,
                [doctor_id],
            )
            working_days = sorted(r[0] for r in cur.fetchall())

            cur.execute(
                """
                SELECT date, is_available
                  FROM doc_availability_overrides
                 WHERE doctor_id = %s
                   AND date >= CURRENT_DATE
                """,
                [doctor_id],
            )
            overrides = {r[0].isoformat(): r[1] for r in cur.fetchall()}

        return Response({"working_days": working_days, "overrides": overrides})


DOCTOR_NESTED_FIELDS = "id, full_name, specialization, clinic_name, clinic_address"


class AppointmentListCreateView(APIView):
    """GET /api/aidoccall/appointments/  - patient's appointments (optionally
    ?status= or ?upcoming=true) with the doctor summary nested.

    POST /api/aidoccall/appointments/ - book a new appointment.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        patient_id, err = _patient_id_or_response(request)
        if err:
            return err

        upcoming = request.query_params.get("upcoming") == "true"
        status_filter = (request.query_params.get("status") or "").strip()

        where = ["a.patient_id = %s"]
        params: list = [patient_id]
        if status_filter:
            where.append("a.status = %s")
            params.append(status_filter)
        if upcoming:
            where.append("a.appointment_date >= CURRENT_DATE")
            where.append("a.status IN ('pending', 'confirmed', 'cancelled')")

        order = "a.appointment_date ASC, a.start_time ASC" if upcoming else "a.appointment_date DESC"

        with connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT a.id, a.appointment_date, a.start_time, a.end_time, a.visit_type,
                       a.status, a.amount, a.payment_status, a.reason_for_visit,
                       a.patient_name, a.patient_email, a.patient_phone,
                       d.id AS doctor_id, d.full_name AS doctor_full_name,
                       d.specialization AS doctor_specialization,
                       d.clinic_name AS doctor_clinic_name,
                       d.clinic_address AS doctor_clinic_address,
                       d.consultation_fee AS doctor_consultation_fee
                  FROM doc_appointments a
                  JOIN doc_doctors d ON d.id = a.doctor_id
                 WHERE {' AND '.join(where)}
                 ORDER BY {order}
                """,
                params,
            )
            rows = _dicts(cur)

        appointments = []
        for row in rows:
            appointments.append(
                {
                    "id": row["id"],
                    "appointment_date": row["appointment_date"],
                    "start_time": row["start_time"],
                    "end_time": row["end_time"],
                    "visit_type": row["visit_type"],
                    "status": row["status"],
                    "amount": row["amount"],
                    "payment_status": row["payment_status"],
                    "reason_for_visit": row["reason_for_visit"],
                    "patient_name": row["patient_name"],
                    "patient_email": row["patient_email"],
                    "patient_phone": row["patient_phone"],
                    "doctor": {
                        "id": row["doctor_id"],
                        "full_name": row["doctor_full_name"],
                        "specialization": row["doctor_specialization"],
                        "clinic_name": row["doctor_clinic_name"],
                        "clinic_address": row["doctor_clinic_address"],
                        "consultation_fee": row["doctor_consultation_fee"],
                    },
                }
            )
        return Response(appointments)

    def post(self, request):
        patient_id, err = _patient_id_or_response(request)
        if err:
            return err

        data = request.data or {}
        doctor_id = str(data.get("doctor_id") or "").strip()
        appointment_date = str(data.get("appointment_date") or "").strip()
        start_time = str(data.get("start_time") or "").strip()

        if not doctor_id or not appointment_date or not start_time:
            return Response(
                {"detail": "doctor_id, appointment_date and start_time are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            _require_uuid(doctor_id)
            date_obj = _require_date(appointment_date)
            hour, minute = (int(part) for part in start_time.split(":")[:2])
        except (ValueError, TypeError):
            return Response(
                {"detail": "doctor_id, appointment_date (YYYY-MM-DD) or start_time (HH:MM) is invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        end_minutes = minute + 30
        end_time = f"{hour + end_minutes // 60:02d}:{end_minutes % 60:02d}:00"
        start_time_full = f"{hour:02d}:{minute:02d}:00"

        patient_name = str(data.get("patient_name") or "").strip()
        patient_email = str(data.get("patient_email") or "").strip() or None
        patient_phone = str(data.get("patient_phone") or "").strip() or None
        visit_type = str(data.get("visit_type") or "online")
        reason = data.get("reason_for_visit") or None
        symptoms = data.get("symptoms") or None
        amount = data.get("amount")

        with connection.cursor() as cur:
            # Booking implies selecting the doctor (patient-doctor relationship).
            cur.execute(
                """
                INSERT INTO doc_patient_doctor_selections (patient_id, doctor_id)
                VALUES (%s, %s)
                ON CONFLICT (patient_id, doctor_id) DO NOTHING
                """,
                [patient_id, doctor_id],
            )

            cur.execute(
                """
                INSERT INTO doc_appointments (
                    patient_id, doctor_id, patient_name, patient_email, patient_phone,
                    appointment_date, start_time, end_time, visit_type, status,
                    amount, payment_status, reason_for_visit, symptoms
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 'pending',
                    %s, 'pending', %s, %s
                )
                RETURNING id, appointment_date, start_time, end_time, visit_type,
                          status, amount, payment_status, reason_for_visit
                """,
                [
                    patient_id,
                    doctor_id,
                    patient_name or None,
                    patient_email,
                    patient_phone,
                    date_obj,
                    start_time_full,
                    end_time,
                    visit_type,
                    amount,
                    reason,
                    symptoms,
                ],
            )
            appointment = _dict(cur)

            cur.execute(
                f"SELECT {DOCTOR_NESTED_FIELDS} FROM doc_doctors WHERE id = %s",
                [doctor_id],
            )
            appointment["doctor"] = _dict(cur)

        return Response(appointment, status=status.HTTP_201_CREATED)


def _owned_appointment_response(cur, appointment_id, patient_id) -> Response | None:
    """404 helper: fetch an appointment scoped to the session patient."""
    cur.execute(
        "SELECT id FROM doc_appointments WHERE id = %s AND patient_id = %s LIMIT 1",
        [appointment_id, patient_id],
    )
    if cur.fetchone() is None:
        return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
    return None


class AppointmentConfirmPaymentView(APIView):
    """PATCH /api/aidoccall/appointments/<id>/confirm-payment/"""

    permission_classes = [IsAuthenticated]

    def patch(self, request, appointment_id):
        patient_id, err = _patient_id_or_response(request)
        if err:
            return err

        with connection.cursor() as cur:
            err = _owned_appointment_response(cur, appointment_id, patient_id)
            if err:
                return err
            cur.execute(
                """
                UPDATE doc_appointments
                   SET payment_status = 'paid', status = 'confirmed', updated_at = now()
                 WHERE id = %s AND patient_id = %s
                """,
                [appointment_id, patient_id],
            )
            cur.execute(
                """
                SELECT id, appointment_date, start_time, end_time, visit_type,
                       status, amount, payment_status
                  FROM doc_appointments
                 WHERE id = %s
                """,
                [appointment_id],
            )
            return Response(_dict(cur))


class AppointmentReceiptView(APIView):
    """GET /api/aidoccall/appointments/<id>/receipt/ - PDF receipt.

    Streams a reportlab-rendered payment receipt, scoped to the session
    patient, and refuses anything that is not payment_status = 'paid'
    (404 foreign/missing, 409 not paid yet).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, appointment_id):
        patient_id, err = _patient_id_or_response(request)
        if err:
            return err

        data = fetch_receipt_data(appointment_id, patient_id)
        if data is None:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        if data["payment_status"] != "paid":
            return Response(
                {"detail": "The receipt is available once the appointment is paid."},
                status=status.HTTP_409_CONFLICT,
            )

        pdf_bytes = build_receipt_pdf(data)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'attachment; filename="receipt-{data["appointment_id"][:8]}.pdf"'
        )
        return response


class AppointmentCancelView(APIView):
    """PATCH /api/aidoccall/appointments/<id>/cancel/  body: {reason}"""

    permission_classes = [IsAuthenticated]

    def patch(self, request, appointment_id):
        patient_id, err = _patient_id_or_response(request)
        if err:
            return err

        reason = (request.data or {}).get("reason") or None

        with connection.cursor() as cur:
            err = _owned_appointment_response(cur, appointment_id, patient_id)
            if err:
                return err
            cur.execute(
                """
                UPDATE doc_appointments
                   SET status = 'cancelled', cancellation_reason = %s,
                       cancelled_by = 'patient', updated_at = now()
                 WHERE id = %s AND patient_id = %s
                """,
                [reason, appointment_id, patient_id],
            )
            cur.execute(
                "SELECT id, status, cancellation_reason, cancelled_by FROM doc_appointments WHERE id = %s",
                [appointment_id],
            )
            return Response(_dict(cur))


class AppointmentRescheduleRequestView(APIView):
    """PATCH /api/aidoccall/appointments/<id>/reschedule-request/  body: {date, time}

    Stores the requested date/time without changing appointment status (the
    status constraint only allows pending/confirmed/completed/cancelled) and
    notifies the doctor in-app.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request, appointment_id):
        patient_id, err = _patient_id_or_response(request)
        if err:
            return err

        data = request.data or {}
        preferred_date = str(data.get("date") or "").strip()
        preferred_time = str(data.get("time") or "").strip()
        if not preferred_date or not preferred_time:
            return Response(
                {"detail": "date and time are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            date_obj = _require_date(preferred_date)
        except ValueError:
            return Response(
                {"detail": "date must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with connection.cursor() as cur:
            err = _owned_appointment_response(cur, appointment_id, patient_id)
            if err:
                return err

            cur.execute(
                """
                SELECT doctor_id FROM doc_appointments WHERE id = %s AND patient_id = %s LIMIT 1
                """,
                [appointment_id, patient_id],
            )
            doctor_id = cur.fetchone()[0]

            cur.execute(
                """
                UPDATE doc_appointments
                   SET requested_reschedule_date = %s,
                       requested_reschedule_time = %s,
                       updated_at = now()
                 WHERE id = %s AND patient_id = %s
                """,
                [date_obj, preferred_time, appointment_id, patient_id],
            )

            date_label = f"{date_obj.day} {date_obj.strftime('%B %Y')}"
            time_label = preferred_time[:5]
            cur.execute(
                """
                INSERT INTO doc_notifications (
                    doctor_id, patient_id, appointment_id, type, channel,
                    status, title, message, is_read
                ) VALUES (
                    %s, %s, %s, 'in_app', 'aidoccall',
                    'sent', 'Reschedule Request',
                    %s, false
                )
                """,
                [
                    doctor_id,
                    patient_id,
                    appointment_id,
                    f"Patient has requested to reschedule their appointment to {date_label} at {time_label}.",
                ],
            )

            cur.execute(
                """
                SELECT id, requested_reschedule_date, requested_reschedule_time, status
                  FROM doc_appointments WHERE id = %s
                """,
                [appointment_id],
            )
            return Response(_dict(cur))
