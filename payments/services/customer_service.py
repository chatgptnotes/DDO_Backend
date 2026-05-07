"""
Stripe Customer lifecycle for AiDocCall patients.

We keep a 1-1 mapping `doc_patients.id <-> stripe.Customer`. Customers are
created lazily — the first time a patient pays, we mint a Stripe customer
and persist the mapping. Subsequent payments reuse it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import connection, transaction

from .stripe_client import get_stripe

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PatientContact:
    patient_id: str
    user_id: str
    email: str | None
    full_name: str | None
    phone: str | None


def load_patient_contact(patient_id: str) -> PatientContact | None:
    """Read minimal patient profile fields needed for the Stripe customer."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, user_id::text, email,
                   COALESCE(NULLIF(TRIM(CONCAT(first_name, ' ', last_name)), ''), email),
                   phone_number
            FROM public.doc_patients
            WHERE id = %s
            LIMIT 1
            """,
            [patient_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        return PatientContact(
            patient_id=row[0],
            user_id=row[1],
            email=row[2],
            full_name=row[3],
            phone=row[4],
        )


def get_or_create_customer(patient_id: str) -> str:
    """Return the Stripe customer id for `patient_id`, creating one if absent.

    Idempotent under concurrent calls thanks to the UNIQUE constraint on
    `doc_stripe_customers.patient_id`: if two requests race, the second
    INSERT raises an integrity error and we re-read the existing row.
    """
    existing = _read_customer_id(patient_id)
    if existing:
        return existing

    contact = load_patient_contact(patient_id)
    if contact is None:
        raise ValueError(f"No doc_patients row for id={patient_id}")

    stripe = get_stripe()
    customer = stripe.Customer.create(
        email=contact.email or None,
        name=contact.full_name or None,
        phone=contact.phone or None,
        metadata={
            "patient_id": contact.patient_id,
            "user_id": contact.user_id,
        },
    )
    logger.info(
        "Created Stripe customer %s for patient %s", customer.id, patient_id
    )

    try:
        _insert_customer(patient_id=patient_id, stripe_customer_id=customer.id, livemode=bool(customer.get("livemode", False)))
        return customer.id
    except Exception:
        # Race: another request already inserted. Re-read and use that id.
        existing = _read_customer_id(patient_id)
        if existing:
            return existing
        raise


def _read_customer_id(patient_id: str) -> str | None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT stripe_customer_id FROM public.doc_stripe_customers "
            "WHERE patient_id = %s LIMIT 1",
            [patient_id],
        )
        row = cur.fetchone()
        return row[0] if row else None


@transaction.atomic
def _insert_customer(*, patient_id: str, stripe_customer_id: str, livemode: bool) -> None:
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.doc_stripe_customers
                (patient_id, stripe_customer_id, livemode)
            VALUES (%s, %s, %s)
            """,
            [patient_id, stripe_customer_id, livemode],
        )
