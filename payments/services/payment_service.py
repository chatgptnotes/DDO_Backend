"""
Core payment orchestration: create or reuse a Stripe PaymentIntent for a
patient's appointment, and persist the resulting state.

Design principles
-----------------
* **Server is the source of truth for money.** Amount + currency are
  derived from the doctor's profile + patient's residency, never from
  the client.
* **Reuse non-terminal intents.** If a PaymentIntent already exists for
  the appointment and is still in a non-terminal state (e.g. the patient
  retried with a new card), we return the same `client_secret` rather
  than creating a duplicate — this is the Stripe-idiomatic pattern.
* **Idempotency.** Stripe's create call uses an idempotency_key derived
  from `appointment_id` + `attempt_number`, so a network retry never
  produces two PaymentIntents.
* **Connect routing.** When `CONNECT_ENABLED`, the PaymentIntent is a
  destination charge — `transfer_data.destination` sends 100% of the payment
  to the doctor's clinic's connected account. Payment creation is blocked for
  a clinic that has not finished Stripe onboarding (`charges_enabled` False).
  A reused intent whose frozen destination no longer matches the clinic's
  current account is canceled and recreated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import connection, transaction

from .customer_service import get_or_create_customer
from .fees import ResolvedFee, resolve_doctor_fee
from .stripe_client import get_stripe

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """Any expected, user-visible payment failure."""


class AppointmentNotFound(PaymentError):
    pass


class NotAppointmentOwner(PaymentError):
    pass


class AppointmentAlreadyPaid(PaymentError):
    pass


class ClinicNotPayable(PaymentError):
    """The doctor's clinic cannot accept payments yet.

    Raised when Connect routing is enabled but the clinic has no connected
    account, or Stripe has not enabled charges on it. The view maps this to
    HTTP 409 — fail fast and legibly rather than letting the charge error at
    confirm time.
    """


@dataclass(frozen=True)
class IntentPayload:
    intent_id: str  # internal UUID
    stripe_payment_intent_id: str
    client_secret: str
    publishable_key: str
    amount_cents: int
    currency: str
    status: str


def create_intent_for_appointment(
    *, appointment_id: str, requester_user_id: str
) -> IntentPayload:
    """Create or reuse a PaymentIntent for an appointment owned by the requester."""
    appt = _load_appointment_for_user(appointment_id, requester_user_id)
    if appt is None:
        raise AppointmentNotFound("Appointment not found or not owned by requester.")

    if appt["payment_status"] == "paid":
        raise AppointmentAlreadyPaid("Appointment is already paid.")

    # Resolve the destination clinic and guard the money path. When Connect is
    # off, `clinic` stays None and payment falls back to the platform account.
    connect_enabled = bool(getattr(settings, "CONNECT_ENABLED", False))
    clinic = None
    if connect_enabled:
        clinic = _load_clinic_for_appointment(appointment_id)
        if clinic is None or not clinic["stripe_account_id"] or not clinic["charges_enabled"]:
            raise ClinicNotPayable(
                "This clinic has not finished payment setup and cannot accept "
                "payments yet."
            )

    # Reuse a non-terminal intent — but only if it still routes to the right
    # clinic. A PaymentIntent's transfer_data is frozen at creation, so if the
    # clinic re-onboarded onto a new account, the stale intent must be replaced.
    existing = _load_latest_intent_for_appointment(appointment_id)
    if existing and existing["status"] not in ("succeeded", "canceled"):
        stale_destination = (
            connect_enabled
            and clinic is not None
            and existing["stripe_connected_account_id"] != clinic["stripe_account_id"]
        )
        if stale_destination:
            logger.info(
                "Intent %s routes to %s but clinic now uses %s — "
                "canceling and recreating",
                existing["stripe_payment_intent_id"],
                existing["stripe_connected_account_id"],
                clinic["stripe_account_id"],
            )
            _cancel_stale_intent(existing["stripe_payment_intent_id"])
        else:
            logger.info(
                "Reusing existing intent %s for appointment %s (status=%s)",
                existing["stripe_payment_intent_id"],
                appointment_id,
                existing["status"],
            )
            # Re-fetch the client_secret from Stripe — we don't store it.
            stripe = get_stripe()
            si = stripe.PaymentIntent.retrieve(existing["stripe_payment_intent_id"])
            return IntentPayload(
                intent_id=str(existing["id"]),
                stripe_payment_intent_id=si.id,
                client_secret=si.client_secret,
                publishable_key=settings.STRIPE_PUBLISHABLE_KEY,
                amount_cents=int(si.amount),
                currency=si.currency,
                status=si.status,
            )

    # Need a fresh intent. Resolve fee + customer, then create it.
    doctor = _load_doctor_for_appointment(appointment_id)
    if doctor is None:
        raise PaymentError("Doctor profile not found for this appointment.")

    fee = resolve_doctor_fee(
        doctor,
        is_indian_resident=bool(appt["is_indian_resident"]),
        visit_type=appt["visit_type"] or "physical",
    )

    stripe_customer_id = get_or_create_customer(appt["patient_id"])
    next_attempt = (existing["attempt_number"] if existing else 0) + 1
    idempotency_key = f"appt_{appointment_id}_v{next_attempt}"

    metadata = {
        "appointment_id": str(appointment_id),
        "patient_id": str(appt["patient_id"]),
        "doctor_id": str(appt["doctor_id"]),
        "visit_type": appt["visit_type"] or "physical",
        "attempt_number": str(next_attempt),
    }
    create_kwargs: dict[str, Any] = dict(
        amount=fee.amount_cents,
        currency=fee.currency,
        customer=stripe_customer_id,
        automatic_payment_methods={"enabled": True},
        description=f"AiDocCall consultation #{appointment_id}",
        metadata=metadata,
        idempotency_key=idempotency_key,
    )

    connected_account_id = None
    if connect_enabled and clinic is not None:
        connected_account_id = clinic["stripe_account_id"]
        metadata["clinic_id"] = str(clinic["clinic_id"])
        # Destination charge: the full amount is transferred to the clinic's
        # connected account. No `application_fee_amount` -> 0% platform cut.
        create_kwargs["transfer_data"] = {"destination": connected_account_id}

    stripe = get_stripe()
    intent = stripe.PaymentIntent.create(**create_kwargs)

    new_id = _insert_intent_row(
        appointment_id=appointment_id,
        patient_id=appt["patient_id"],
        stripe_intent=intent,
        stripe_customer_id=stripe_customer_id,
        attempt_number=next_attempt,
        idempotency_key=idempotency_key,
        fee=fee,
        clinic_id=clinic["clinic_id"] if clinic else None,
        connected_account_id=connected_account_id,
    )

    # Persist the resolved currency back onto the appointment for reporting.
    _set_appointment_currency_and_amount(
        appointment_id=appointment_id,
        amount_cents=fee.amount_cents,
        currency=fee.currency,
    )

    return IntentPayload(
        intent_id=new_id,
        stripe_payment_intent_id=intent.id,
        client_secret=intent.client_secret,
        publishable_key=settings.STRIPE_PUBLISHABLE_KEY,
        amount_cents=fee.amount_cents,
        currency=fee.currency,
        status=intent.status,
    )


def get_intent_for_user(*, intent_id: str, requester_user_id: str) -> dict[str, Any] | None:
    """Return owner-scoped intent state, or None if not found / not owned."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT pi.id::text,
                   pi.stripe_payment_intent_id,
                   pi.status,
                   pi.amount_cents,
                   pi.currency,
                   pi.last_payment_error,
                   pi.appointment_id::text,
                   a.payment_status,
                   a.meeting_link
            FROM public.doc_payment_intents pi
            JOIN public.doc_appointments a ON a.id = pi.appointment_id
            JOIN public.doc_patients p ON p.id = pi.patient_id
            WHERE pi.id = %s AND p.user_id = %s
            LIMIT 1
            """,
            [intent_id, requester_user_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "intent_id": row[0],
            "stripe_payment_intent_id": row[1],
            "status": row[2],
            "amount_cents": row[3],
            "currency": row[4],
            "last_payment_error": row[5],
            "appointment_id": row[6],
            "appointment_payment_status": row[7],
            "meeting_link": row[8],
        }


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------

def _load_appointment_for_user(appointment_id: str, user_id: str) -> dict | None:
    """Resolve appointment and verify the requester owns it via JWT user_id."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT a.id::text,
                   a.patient_id::text,
                   a.doctor_id::text,
                   a.visit_type,
                   a.amount,
                   a.payment_status,
                   p.is_indian_resident
            FROM public.doc_appointments a
            JOIN public.doc_patients p ON p.id = a.patient_id
            WHERE a.id = %s AND p.user_id = %s
            LIMIT 1
            """,
            [appointment_id, user_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "patient_id": row[1],
            "doctor_id": row[2],
            "visit_type": row[3],
            "amount": row[4],
            "payment_status": row[5],
            "is_indian_resident": row[6],
        }


def _load_doctor_for_appointment(appointment_id: str) -> dict | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT d.id::text,
                   d.consultation_fee,
                   d.online_fee,
                   d.consultation_fee_inr,
                   d.online_fee_inr,
                   d.consultation_fee_usd,
                   d.online_fee_usd
            FROM public.doc_appointments a
            JOIN public.doc_doctors d ON d.id = a.doctor_id
            WHERE a.id = %s
            LIMIT 1
            """,
            [appointment_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        keys = (
            "id",
            "consultation_fee",
            "online_fee",
            "consultation_fee_inr",
            "online_fee_inr",
            "consultation_fee_usd",
            "online_fee_usd",
        )
        return dict(zip(keys, row))


def _load_clinic_for_appointment(appointment_id: str) -> dict | None:
    """Resolve the clinic that should receive payment for an appointment.

    Joins appointment -> doctor -> clinic. Returns None if the doctor is not
    attached to any clinic. `stripe_account_id` / `charges_enabled` reflect the
    clinic's current Connect onboarding state.
    """
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT c.id::text,
                   c.stripe_account_id,
                   c.charges_enabled,
                   c.onboarding_status
            FROM public.doc_appointments a
            JOIN public.doc_doctors d ON d.id = a.doctor_id
            JOIN public.clinics c ON c.id = d.clinic_id
            WHERE a.id = %s
            LIMIT 1
            """,
            [appointment_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "clinic_id": row[0],
            "stripe_account_id": row[1],
            "charges_enabled": bool(row[2]),
            "onboarding_status": row[3],
        }


def _load_latest_intent_for_appointment(appointment_id: str) -> dict | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id::text,
                   stripe_payment_intent_id,
                   status,
                   attempt_number,
                   stripe_connected_account_id
            FROM public.doc_payment_intents
            WHERE appointment_id = %s
            ORDER BY attempt_number DESC
            LIMIT 1
            """,
            [appointment_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "stripe_payment_intent_id": row[1],
            "status": row[2],
            "attempt_number": row[3],
            "stripe_connected_account_id": row[4],
        }


def _cancel_stale_intent(stripe_payment_intent_id: str) -> None:
    """Cancel a PaymentIntent whose destination no longer matches the clinic.

    Best-effort on the Stripe side — the intent may already be uncancelable —
    but the local row is always flipped to `canceled` so it is not reused.
    """
    stripe = get_stripe()
    try:
        stripe.PaymentIntent.cancel(stripe_payment_intent_id)
    except Exception as exc:  # noqa: BLE001 — proceed; a fresh intent replaces it
        logger.warning(
            "Could not cancel stale intent %s: %s", stripe_payment_intent_id, exc
        )
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.doc_payment_intents
               SET status = 'canceled'
             WHERE stripe_payment_intent_id = %s
               AND status NOT IN ('succeeded', 'canceled')
            """,
            [stripe_payment_intent_id],
        )


@transaction.atomic
def _insert_intent_row(
    *,
    appointment_id: str,
    patient_id: str,
    stripe_intent,
    stripe_customer_id: str,
    attempt_number: int,
    idempotency_key: str,
    fee: ResolvedFee,
    clinic_id: str | None = None,
    connected_account_id: str | None = None,
) -> str:
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.doc_payment_intents
                (appointment_id, patient_id,
                 stripe_payment_intent_id, stripe_customer_id,
                 amount_cents, currency, status,
                 attempt_number, idempotency_key, metadata,
                 clinic_id, stripe_connected_account_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            RETURNING id::text
            """,
            [
                appointment_id,
                patient_id,
                stripe_intent.id,
                stripe_customer_id,
                fee.amount_cents,
                fee.currency,
                stripe_intent.status,
                attempt_number,
                idempotency_key,
                "{}",
                clinic_id,
                connected_account_id,
            ],
        )
        return cur.fetchone()[0]


def _set_appointment_currency_and_amount(
    *, appointment_id: str, amount_cents: int, currency: str
) -> None:
    """Snapshot the resolved fee onto doc_appointments for reporting."""
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.doc_appointments
               SET amount = %s,
                   currency = %s
             WHERE id = %s
            """,
            [amount_cents / 100, currency, appointment_id],
        )
