"""
Stripe webhook handler — platform + Connect streams.

Two endpoints feed this module:
  * the platform webhook  (/api/payments/webhooks/stripe/)        stream="platform"
  * the Connect webhook   (/api/payments/webhooks/stripe/connect/) stream="connect"
Each has its own signing secret. Connected-account events (`account.updated`)
arrive on the Connect stream; `payment_intent.*` arrive on the platform stream
even for destination charges, because the charge lives on the platform account.

Responsibilities
----------------
1. Verify the `Stripe-Signature` header against the raw body using the
   stream's secret. Mismatch / missing secret -> fail closed.
2. Record the event (`doc_stripe_webhook_events`, UNIQUE on `stripe_event_id`).
3. Dispatch by event type. **All handlers are idempotent** — they may be
   re-run safely.
4. Mark the event `processed_at = NOW()` only after the handler succeeds.

Reliability
-----------
Dedup is **process-first, not record-first**. The event row is recorded up
front for audit, but an event counts as "done" only once `processed_at` is
set. A redelivery of an event whose `processed_at` is still NULL is
**reprocessed**, not skipped — so a transient handler failure can never
silently drop fulfillment. On handler failure the error is recorded and the
exception re-raised (Stripe retries), with `processed_at` left NULL.
Correctness under a concurrent double-delivery rests on every handler being
**idempotent** — re-running one is a no-op — rather than on a lock.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from django.conf import settings
from django.db import connection

from .connect_service import sync_account_from_stripe
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


_SECRET_SETTING = {
    "platform": "STRIPE_WEBHOOK_SECRET",
    "connect": "STRIPE_CONNECT_WEBHOOK_SECRET",
}


def handle_webhook(
    *, raw_body: bytes, signature_header: str | None, stream: str = "platform"
) -> WebhookResult:
    """Top-level entrypoint called from the DRF webhook views.

    `stream` selects the signing secret and handler table — "platform" or
    "connect".
    """
    secret = _secret_for_stream(stream)
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
    handlers = _HANDLERS if stream == "platform" else _CONNECT_HANDLERS

    # 1. Record the event for audit + idempotency. ON CONFLICT keeps a
    #    redelivery from erroring; whether it is a true duplicate is decided
    #    next by `processed_at`, not by row existence.
    _record_event(event_id=event_id, event_type=event_type, event=event)

    # 2. Process-first dedup: skip only if a PRIOR delivery already finished.
    #    A redelivery whose `processed_at` is still NULL is reprocessed — every
    #    handler is idempotent, so an earlier transient failure is recovered.
    if _already_processed(event_id):
        logger.info(
            "Webhook %s (%s) already processed — skipping", event_id, event_type
        )
        return WebhookResult(True, event_type, event_id)

    # 3. Dispatch. On failure, record the error and re-raise so Stripe retries;
    #    `processed_at` stays NULL, so the retry reprocesses the event.
    try:
        _dispatch(event_type=event_type, event=event, handlers=handlers)
    except Exception as exc:  # noqa: BLE001 — record + propagate so Stripe retries
        logger.exception("Webhook %s (%s) handler failed: %s", event_id, event_type, exc)
        _mark_event_failed(event_id=event_id, error=str(exc))
        raise

    _mark_event_processed(event_id=event_id)
    return WebhookResult(False, event_type, event_id)


def _secret_for_stream(stream: str) -> str:
    setting_name = _SECRET_SETTING.get(stream)
    if setting_name is None:
        raise ValueError(f"Unknown webhook stream: {stream!r}")
    secret = getattr(settings, setting_name, "")
    if not secret:
        raise RuntimeError(
            f"{setting_name} is not configured. "
            f"Set it before exposing the {stream} webhook endpoint."
        )
    return secret


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------

def _dispatch(
    *, event_type: str, event: dict[str, Any], handlers: dict[str, Callable]
) -> None:
    handler = handlers.get(event_type)
    if not handler:
        logger.info("Webhook event type %s is not handled — acknowledging", event_type)
        return
    handler(event)


# ---- Platform stream: payment lifecycle ------------------------------------

def apply_payment_intent_state(intent_obj: dict[str, Any]) -> None:
    """Bring local state in line with a Stripe PaymentIntent object.

    Shared by the `payment_intent.*` webhook handlers and the `reconcile_stripe`
    sweep — the sweep passes a freshly retrieved PaymentIntent, the webhook
    passes the event's object. Idempotent: safe to re-run, and the
    terminal-status guard in `_update_intent_status` blocks regressions.
    """
    pi_id = intent_obj["id"]
    status = intent_obj.get("status") or "requires_payment_method"

    if status == "succeeded":
        _update_intent_status(
            stripe_payment_intent_id=pi_id,
            status="succeeded",
            succeeded=True,
            last_error=None,
        )
        # Best-effort: record the destination transfer for audit / refund
        # tracing. Never let this fail fulfillment — observability, not money.
        _store_transfer_id(pi_id, intent_obj.get("latest_charge"))

        appointment_id = (intent_obj.get("metadata") or {}).get("appointment_id")
        if not appointment_id:
            logger.error(
                "PaymentIntent %s succeeded but has no appointment_id metadata", pi_id
            )
            return
        fulfill_paid_appointment(
            appointment_id=appointment_id,
            stripe_payment_intent_id=pi_id,
            amount_cents=int(intent_obj["amount"]),
            currency=intent_obj["currency"],
        )
    elif status == "canceled":
        _update_intent_status(
            stripe_payment_intent_id=pi_id,
            status="canceled",
            succeeded=False,
            last_error=None,
        )
    else:
        _update_intent_status(
            stripe_payment_intent_id=pi_id,
            status=status,
            succeeded=False,
            last_error=intent_obj.get("last_payment_error"),
        )


def _on_payment_intent_event(event: dict[str, Any]) -> None:
    """Handle payment_intent.succeeded / payment_failed / canceled uniformly."""
    apply_payment_intent_state(event["data"]["object"])


def _on_charge_refunded(event: dict[str, Any]) -> None:
    charge = event["data"]["object"]
    pi_id = charge.get("payment_intent")
    if not pi_id:
        logger.warning("charge.refunded with no payment_intent — skipping")
        return
    appointment_id = _appointment_id_for_intent(pi_id)
    if appointment_id:
        mark_appointment_refunded(appointment_id=appointment_id)


# ---- Connect stream: connected-account lifecycle ---------------------------

def _on_account_updated(event: dict[str, Any]) -> None:
    """Sync a connected account's capabilities onto its clinic row.

    The event payload can be stale or out of order, so we re-fetch the account
    for authoritative state and pass the event's `created` time as an
    out-of-order guard (older events are ignored downstream).
    """
    account_obj = event["data"]["object"]
    account_id = account_obj.get("id")
    if not account_id:
        logger.warning("account.updated with no account id — skipping")
        return

    event_created_at = datetime.fromtimestamp(int(event["created"]), tz=timezone.utc)
    try:
        account = get_stripe().Account.retrieve(account_id)
    except Exception as exc:  # noqa: BLE001 — fall back to the (possibly stale) payload
        logger.warning(
            "Account.retrieve(%s) failed: %s — using event payload", account_id, exc
        )
        account = account_obj

    sync_account_from_stripe(account, event_created_at=event_created_at)


def _on_account_deauthorized(event: dict[str, Any]) -> None:
    """A connected account was deauthorized — mark the clinic disabled."""
    account_id = event.get("account") or (event["data"]["object"] or {}).get("id")
    if not account_id:
        return
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.clinics
               SET onboarding_status  = 'disabled',
                   charges_enabled    = FALSE,
                   payouts_enabled    = FALSE,
                   account_updated_at = NOW()
             WHERE stripe_account_id = %s
            """,
            [account_id],
        )
    logger.warning("Connected account %s deauthorized — clinic disabled", account_id)


_HANDLERS: dict[str, Callable] = {
    "payment_intent.succeeded": _on_payment_intent_event,
    "payment_intent.payment_failed": _on_payment_intent_event,
    "payment_intent.canceled": _on_payment_intent_event,
    "charge.refunded": _on_charge_refunded,
}

_CONNECT_HANDLERS: dict[str, Callable] = {
    "account.updated": _on_account_updated,
    "account.application.deauthorized": _on_account_deauthorized,
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _record_event(*, event_id: str, event_type: str, event: dict[str, Any]) -> None:
    """Append the event to the idempotency log. No-op if already recorded."""
    livemode = bool(event.get("livemode", False))
    payload_json = json.dumps(event)
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.doc_stripe_webhook_events
                (stripe_event_id, event_type, livemode, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (stripe_event_id) DO NOTHING
            """,
            [event_id, event_type, livemode, payload_json],
        )


def _already_processed(event_id: str) -> bool:
    """True iff a prior delivery of this event finished successfully.

    A row with `processed_at IS NULL` is a *seen-but-unfinished* event (the
    earlier delivery failed mid-dispatch) — that is reprocessed, not skipped.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT processed_at FROM public.doc_stripe_webhook_events "
            "WHERE stripe_event_id = %s",
            [event_id],
        )
        row = cur.fetchone()
        return bool(row and row[0] is not None)


def _mark_event_processed(*, event_id: str) -> None:
    """Mark the event done. Until this runs, a redelivery is reprocessed."""
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
    """Persist a dispatch failure. `processed_at` stays NULL, so Stripe's retry
    reprocesses the event."""
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
    """Update a PaymentIntent row's status.

    Guarded so a late / out-of-order event can never move an intent *out of* a
    terminal status (e.g. a delayed `payment_failed` after `succeeded`). The
    write applies only when the current status is non-terminal, or the new
    status is itself terminal.
    """
    last_error_json = json.dumps(last_error) if last_error else None
    terminal = ["succeeded", "canceled"]
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.doc_payment_intents
               SET status = %s,
                   last_payment_error = %s::jsonb,
                   succeeded_at = CASE WHEN %s THEN NOW() ELSE succeeded_at END
             WHERE stripe_payment_intent_id = %s
               AND (status <> ALL(%s) OR %s = ANY(%s))
            """,
            [
                status,
                last_error_json,
                succeeded,
                stripe_payment_intent_id,
                terminal,
                status,
                terminal,
            ],
        )


def _store_transfer_id(stripe_payment_intent_id: str, latest_charge: Any) -> None:
    """Record the destination-charge transfer id on the intent row (audit)."""
    if not latest_charge:
        return
    try:
        charge = get_stripe().Charge.retrieve(latest_charge)
        transfer_id = charge.get("transfer")
        if not transfer_id:
            return
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE public.doc_payment_intents SET transfer_id = %s "
                "WHERE stripe_payment_intent_id = %s",
                [transfer_id, stripe_payment_intent_id],
            )
    except Exception as exc:  # noqa: BLE001 — observability only, never blocks fulfillment
        logger.warning(
            "Could not record transfer id for %s: %s", stripe_payment_intent_id, exc
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
