"""
Fulfillment adapter — keeps the `payments` app domain-agnostic.

The webhook handler calls `fulfill_paid_appointment(appointment_id, ...)`
here. This module lazy-imports the concrete `aidoccall.services.fulfill_appointment`
implementation, so `payments` never has a hard import-time dependency on
clinical-flow code. Future apps (e.g. surgeonpilot subscriptions) can hook in
the same way.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def fulfill_paid_appointment(
    *,
    appointment_id: str,
    stripe_payment_intent_id: str,
    amount_cents: int,
    currency: str,
) -> None:
    """Mark the appointment paid + send confirmation email. Idempotent."""
    # Lazy import to avoid coupling the payments app to aidoccall at import-time.
    from aidoccall.services.fulfillment import fulfill_appointment

    fulfill_appointment(
        appointment_id=appointment_id,
        stripe_payment_intent_id=stripe_payment_intent_id,
        amount_cents=amount_cents,
        currency=currency,
    )


def mark_appointment_refunded(*, appointment_id: str) -> None:
    """Flip appointment to refunded status (called from charge.refunded webhook)."""
    from aidoccall.services.fulfillment import refund_appointment

    refund_appointment(appointment_id=appointment_id)
