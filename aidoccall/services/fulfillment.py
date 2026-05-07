"""
Appointment fulfillment after payment.

Called from the Stripe webhook handler in `payments.services.webhook_handler`.

`fulfill_appointment` is the only place that flips `doc_appointments.payment_status`
to `'paid'` once Stripe is wired up. The legacy `confirmPayment` path in the
frontend is going away — see CHANGELOG.

The function is **idempotent**: re-running for the same appointment does not
re-send email and does not double-update the row.
"""
from __future__ import annotations

import logging

from django.db import connection, transaction

from payments.services.email import send_payment_confirmation

logger = logging.getLogger(__name__)


@transaction.atomic
def fulfill_appointment(
    *,
    appointment_id: str,
    stripe_payment_intent_id: str,
    amount_cents: int,
    currency: str,
) -> None:
    """Mark appointment paid and email the patient. Idempotent."""
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.doc_appointments
               SET payment_status = 'paid',
                   status = CASE WHEN status = 'pending' THEN 'confirmed' ELSE status END,
                   payment_id = %s,
                   updated_at = NOW()
             WHERE id = %s
               AND payment_status <> 'paid'
            RETURNING id
            """,
            [stripe_payment_intent_id, appointment_id],
        )
        updated = cur.fetchone()

    if not updated:
        logger.info(
            "Appointment %s already fulfilled; skipping email", appointment_id
        )
        return

    context = _load_email_context(appointment_id)
    if context is None:
        logger.warning(
            "Appointment %s fulfilled but context lookup failed; skipping email",
            appointment_id,
        )
        return

    send_payment_confirmation(
        to_email=context["patient_email"],
        patient_name=context["patient_name"],
        doctor_name=context["doctor_name"],
        appointment_date=context["appointment_date"],
        start_time=context["start_time"],
        visit_type=context["visit_type"],
        meeting_link=context["meeting_link"],
        amount_cents=amount_cents,
        currency=currency,
    )


def refund_appointment(*, appointment_id: str) -> None:
    """Flip an appointment to refunded. Idempotent."""
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.doc_appointments
               SET payment_status = 'refunded',
                   updated_at = NOW()
             WHERE id = %s
               AND payment_status <> 'refunded'
            """,
            [appointment_id],
        )
    logger.info("Appointment %s marked refunded", appointment_id)


def _load_email_context(appointment_id: str) -> dict | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT a.appointment_date,
                   a.start_time,
                   a.visit_type,
                   a.meeting_link,
                   p.email,
                   COALESCE(NULLIF(TRIM(CONCAT(p.first_name, ' ', p.last_name)), ''), p.email),
                   d.full_name
            FROM public.doc_appointments a
            LEFT JOIN public.doc_patients p ON p.id = a.patient_id
            LEFT JOIN public.doc_doctors d ON d.id = a.doctor_id
            WHERE a.id = %s
            LIMIT 1
            """,
            [appointment_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "appointment_date": row[0],
            "start_time": row[1],
            "visit_type": row[2] or "physical",
            "meeting_link": row[3],
            "patient_email": row[4],
            "patient_name": row[5],
            "doctor_name": row[6],
        }
