"""
Stripe webhook handler.

Responsibilities
----------------
1. Verify the `Stripe-Signature` header against the raw request body using
   `STRIPE_WEBHOOK_SECRET`. Mismatch -> 400.
2. Persist the event (UNIQUE on `stripe_event_id` for idempotency). If the
   event has already been seen, return 200 immediately — Stripe retries are
   safe.
3. Dispatch by event type to a small set of handlers.
4. Mark the event row as `processed_at = NOW()` once handler completes.
   On handler failure, persist `processing_error` and re-raise so Stripe
   retries (the dedup row keeps the side-effects from running twice).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import IntegrityError, connection, transaction

from .fulfillment import fulfill_paid_appointment, mark_appointment_refunded
from .stripe_client import get_stripe

logger = logging.getLogger(__name__)


class WebhookSignatureError(Exception):
    """Raised when the Stripe-Signature header is missing or invalid."""


@dataclass(frozen=True)
class WebhookResult:
    duplicate: bool
    event_type: str
    event_id: str


def handle_webhook(*, raw_body: bytes, signature_header: str | None) -> WebhookResult:
    """Top-level entrypoint called from the DRF view."""
    secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET is not configured. "
            "Set it before exposing the webhook endpoint."
        )

    if not signature_header:
        raise WebhookSignatureError("Missing Stripe-Signature header")

    stripe = get_stripe()
    try:
        event = stripe.Webhook.construct_event(
            payload=raw_body, sig_header=signature_header, secret=secret
        )
    except stripe.error.SignatureVerificationError as exc:
        raise WebhookSignatureError(f"Invalid signature: {exc}") from exc
    except ValueError as exc:
        raise WebhookSignatureError(f"Malformed webhook payload: {exc}") from exc

    event_id = event["id"]
    event_type = event["type"]

    # 1. Persist the event for idempotency. Duplicate -> short-circuit success.
    duplicate = _record_event_or_duplicate(event_id=event_id, event_type=event_type, event=event)
    if duplicate:
        logger.info("Stripe webhook %s (%s) is a duplicate — skipping", event_id, event_type)
        return WebhookResult(duplicate=True, event_type=event_type, event_id=event_id)

    # 2. Dispatch.
    try:
        _dispatch(event_type=event_type, event=event)
    except Exception as exc:  # noqa: BLE001 — log and propagate so Stripe retries
        logger.exception("Webhook %s (%s) handler failed: %s", event_id, event_type, exc)
        _mark_event_failed(event_id=event_id, error=str(exc))
        raise

    _mark_event_processed(event_id=event_id)
    return WebhookResult(duplicate=False, event_type=event_type, event_id=event_id)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

def _dispatch(*, event_type: str, event: dict[str, Any]) -> None:
    handler = _HANDLERS.get(event_type)
    if not handler:
        logger.info("Webhook event type %s is not handled — acknowledging", event_type)
        return
    handler(event)


def _on_payment_intent_succeeded(event: dict[str, Any]) -> None:
    intent_obj = event["data"]["object"]
    pi_id = intent_obj["id"]
    appointment_id = (intent_obj.get("metadata") or {}).get("appointment_id")
    if not appointment_id:
        logger.error("PaymentIntent %s succeeded but has no appointment_id metadata", pi_id)
        return

    _update_intent_status(
        stripe_payment_intent_id=pi_id,
        status="succeeded",
        succeeded=True,
        last_error=None,
    )
    fulfill_paid_appointment(
        appointment_id=appointment_id,
        stripe_payment_intent_id=pi_id,
        amount_cents=int(intent_obj["amount"]),
        currency=intent_obj["currency"],
    )


def _on_payment_intent_failed(event: dict[str, Any]) -> None:
    intent_obj = event["data"]["object"]
    _update_intent_status(
        stripe_payment_intent_id=intent_obj["id"],
        status=intent_obj.get("status", "requires_payment_method"),
        succeeded=False,
        last_error=intent_obj.get("last_payment_error"),
    )


def _on_payment_intent_canceled(event: dict[str, Any]) -> None:
    intent_obj = event["data"]["object"]
    _update_intent_status(
        stripe_payment_intent_id=intent_obj["id"],
        status="canceled",
        succeeded=False,
        last_error=None,
    )


def _on_charge_refunded(event: dict[str, Any]) -> None:
    charge = event["data"]["object"]
    pi_id = charge.get("payment_intent")
    if not pi_id:
        logger.warning("charge.refunded with no payment_intent — skipping")
        return
    appointment_id = _appointment_id_for_intent(pi_id)
    if appointment_id:
        mark_appointment_refunded(appointment_id=appointment_id)


_HANDLERS = {
    "payment_intent.succeeded": _on_payment_intent_succeeded,
    "payment_intent.payment_failed": _on_payment_intent_failed,
    "payment_intent.canceled": _on_payment_intent_canceled,
    "charge.refunded": _on_charge_refunded,
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _record_event_or_duplicate(*, event_id: str, event_type: str, event: dict[str, Any]) -> bool:
    """INSERT the event row. Returns True if the event was already recorded."""
    livemode = bool(event.get("livemode", False))
    payload_json = json.dumps(event)
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.doc_stripe_webhook_events
                        (stripe_event_id, event_type, livemode, payload)
                    VALUES (%s, %s, %s, %s::jsonb)
                    """,
                    [event_id, event_type, livemode, payload_json],
                )
        return False
    except IntegrityError:
        return True


def _mark_event_processed(*, event_id: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.doc_stripe_webhook_events
               SET processed_at = NOW(),
                   processing_error = NULL
             WHERE stripe_event_id = %s
            """,
            [event_id],
        )


def _mark_event_failed(*, event_id: str, error: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.doc_stripe_webhook_events
               SET processing_error = %s
             WHERE stripe_event_id = %s
            """,
            [error[:2000], event_id],
        )


def _update_intent_status(
    *,
    stripe_payment_intent_id: str,
    status: str,
    succeeded: bool,
    last_error: dict | None,
) -> None:
    last_error_json = json.dumps(last_error) if last_error else None
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.doc_payment_intents
               SET status = %s,
                   last_payment_error = %s::jsonb,
                   succeeded_at = CASE WHEN %s THEN NOW() ELSE succeeded_at END
             WHERE stripe_payment_intent_id = %s
            """,
            [status, last_error_json, succeeded, stripe_payment_intent_id],
        )


def _appointment_id_for_intent(stripe_payment_intent_id: str) -> str | None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT appointment_id::text FROM public.doc_payment_intents "
            "WHERE stripe_payment_intent_id = %s LIMIT 1",
            [stripe_payment_intent_id],
        )
        row = cur.fetchone()
        return row[0] if row else None
