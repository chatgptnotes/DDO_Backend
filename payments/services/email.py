"""
Transactional email for payment events.

v1 ships a single email: payment confirmation containing the doctor's
already-uploaded `meeting_link`. The email is best-effort — failure is logged
but does not block fulfillment, because the meeting link is also visible in
the patient portal.

Uses Django's `send_mail`, so the choice of backend (SMTP, console, Resend
via django-anymail) is decided in `config/settings/base.py` via env vars.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_payment_confirmation(
    *,
    to_email: str | None,
    patient_name: str | None,
    doctor_name: str | None,
    appointment_date,
    start_time,
    visit_type: str,
    meeting_link: str | None,
    amount_cents: int,
    currency: str,
) -> None:
    """Send a confirmation email to the patient. Logs and swallows on failure."""
    if not to_email:
        logger.warning("Cannot send payment confirmation: no email on file")
        return

    amount_major = amount_cents / 100
    currency_upper = currency.upper()
    subject = "Your AiDocCall consultation is confirmed"
    lines = [
        f"Hi {patient_name or 'there'},",
        "",
        "Your payment was successful and your consultation is confirmed.",
        "",
        f"Doctor: {doctor_name or 'Your doctor'}",
        f"Date: {appointment_date}",
        f"Time: {start_time}",
        f"Type: {visit_type}",
        f"Amount: {amount_major:.2f} {currency_upper}",
        "",
    ]
    if meeting_link and visit_type == "online":
        lines.extend(
            [
                "Join your consultation here:",
                meeting_link,
                "",
            ]
        )
    elif visit_type == "online":
        lines.extend(
            [
                "Your doctor will share the meeting link shortly. "
                "You can also see it in your AiDocCall dashboard.",
                "",
            ]
        )

    lines.extend(
        [
            "Thank you,",
            "The AiDocCall team",
        ]
    )

    body = "\n".join(lines)
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@aidoccall.com")

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=from_email,
            recipient_list=[to_email],
            fail_silently=False,
        )
        logger.info("Payment confirmation email sent to %s", to_email)
    except Exception as exc:  # noqa: BLE001 — best-effort; never block fulfillment
        logger.exception("Failed to send payment confirmation email to %s: %s", to_email, exc)
