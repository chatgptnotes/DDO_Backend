"""
Refunds for paid consultations.

The platform is the merchant of record (destination charges), so the platform
issues the refund. `reverse_transfer=True` is essential: it reverses the
destination transfer, clawing the money back from the clinic's connected
account — otherwise the platform refunds the patient while the clinic keeps the
funds.

This module only *initiates* the refund. The appointment is flipped to
`refunded` by the `charge.refunded` webhook (see `webhook_handler.py`), keeping
Stripe the single source of truth for payment state.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import connection

from .stripe_client import get_stripe

logger = logging.getLogger(__name__)


class RefundError(Exception):
    """Any expected, user-visible refund failure."""


class NoRefundablePayment(RefundError):
    pass


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    status: str
    amount_cents: int
    currency: str
    reversed_transfer: bool


def refund_appointment_payment(
    *, appointment_id: str, amount_cents: int | None = None
) -> RefundResult:
    """Refund the succeeded payment for an appointment.

    `amount_cents=None` issues a full refund. For a destination charge the
    transfer is reversed proportionally so the clinic gives back its share.
    Idempotent-friendly: if the appointment is already refunded, raises
    `NoRefundablePayment` rather than double-refunding.
    """
    intent = _load_succeeded_intent(appointment_id)
    if intent is None:
        raise NoRefundablePayment(
            "No completed payment was found for this appointment."
        )
    if intent["payment_status"] == "refunded":
        raise NoRefundablePayment("This appointment has already been refunded.")

    # `reverse_transfer` is valid only for destination charges. A legacy
    # platform-account charge (no connected account) must not pass it.
    is_destination_charge = bool(intent["stripe_connected_account_id"])

    refund_kwargs: dict = {
        "payment_intent": intent["stripe_payment_intent_id"],
        "reverse_transfer": is_destination_charge,
    }
    if amount_cents is not None:
        if amount_cents <= 0 or amount_cents > intent["amount_cents"]:
            raise RefundError("Refund amount is outside the payable range.")
        refund_kwargs["amount"] = amount_cents

    stripe = get_stripe()
    refund = stripe.Refund.create(**refund_kwargs)
    logger.info(
        "Refund %s created for appointment %s (intent=%s, reverse_transfer=%s)",
        refund.id,
        appointment_id,
        intent["stripe_payment_intent_id"],
        is_destination_charge,
    )

    return RefundResult(
        refund_id=refund.id,
        status=refund.status,
        amount_cents=int(refund.amount),
        currency=refund.currency,
        reversed_transfer=is_destination_charge,
    )


def _load_succeeded_intent(appointment_id: str) -> dict | None:
    """Most recent succeeded PaymentIntent for an appointment, with its
    appointment payment_status (so we can refuse a double refund)."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT pi.stripe_payment_intent_id,
                   pi.amount_cents,
                   pi.currency,
                   pi.stripe_connected_account_id,
                   a.payment_status
            FROM public.doc_payment_intents pi
            JOIN public.doc_appointments a ON a.id = pi.appointment_id
            WHERE pi.appointment_id = %s
              AND pi.status = 'succeeded'
            ORDER BY pi.succeeded_at DESC NULLS LAST, pi.attempt_number DESC
            LIMIT 1
            """,
            [appointment_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "stripe_payment_intent_id": row[0],
            "amount_cents": row[1],
            "currency": row[2],
            "stripe_connected_account_id": row[3],
            "payment_status": row[4],
        }
