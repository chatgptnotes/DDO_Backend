"""
Read-only Django models for the Stripe payments tables.

`managed = False` everywhere — Supabase owns the schema. The CREATE TABLE
statements live in `aidoccall.com/supabase/migrations/20260504_stripe_payments.sql`
and `backend/supabase/migrations/20260520_create_clinics.sql` (Stripe Connect).

We use raw UUIDFields rather than ForeignKey so:
  * we don't fight Django's relational machinery on `managed=False` tables, and
  * serializers can output `appointment_id`, `patient_id` exactly as PostgREST does.
"""
from __future__ import annotations

from django.db import models


class Clinic(models.Model):
    """Mirror of `public.clinics` — the Stripe Connect tenant.

    One clinic holds at most one Express connected account. A patient payment
    for a doctor routes to that doctor's clinic's connected account via a
    destination charge. The capability booleans (`charges_enabled` etc.) are a
    denormalised mirror of the Stripe account, kept fresh by the
    `account.updated` webhook and the `reconcile_stripe` sweep.

    CREATE TABLE lives in `backend/supabase/migrations/20260520_create_clinics.sql`.
    """

    id = models.UUIDField(primary_key=True)
    name = models.TextField()
    contact_email = models.TextField(blank=True, null=True)
    country = models.CharField(max_length=2, blank=True, null=True)
    created_by = models.UUIDField(blank=True, null=True)

    stripe_account_id = models.TextField(unique=True, blank=True, null=True)
    stripe_livemode = models.BooleanField(default=False)
    onboarding_status = models.TextField(default="not_started")
    charges_enabled = models.BooleanField(default=False)
    payouts_enabled = models.BooleanField(default=False)
    details_submitted = models.BooleanField(default=False)
    requirements_due = models.JSONField(default=dict)
    default_currency = models.CharField(max_length=3, blank=True, null=True)
    account_updated_at = models.DateTimeField(blank=True, null=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    @property
    def is_payable(self) -> bool:
        """True iff the clinic can accept patient payments right now.

        The money path checks this — never `onboarding_status`, which is a
        derived display value.
        """
        return bool(self.stripe_account_id) and self.charges_enabled

    class Meta:
        managed = False
        db_table = "clinics"

    def __str__(self) -> str:
        return self.name


class StripeCustomer(models.Model):
    """Mirror of `public.doc_stripe_customers`."""

    id = models.UUIDField(primary_key=True)
    patient_id = models.UUIDField(unique=True)
    stripe_customer_id = models.TextField(unique=True)
    livemode = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "doc_stripe_customers"


class PaymentIntent(models.Model):
    """Mirror of `public.doc_payment_intents` — Stripe PaymentIntent state."""

    id = models.UUIDField(primary_key=True)
    appointment_id = models.UUIDField()
    patient_id = models.UUIDField()
    stripe_payment_intent_id = models.TextField(unique=True)
    stripe_customer_id = models.TextField(blank=True, null=True)
    amount_cents = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.TextField()
    attempt_number = models.IntegerField(default=1)
    idempotency_key = models.TextField(unique=True)
    last_payment_error = models.JSONField(blank=True, null=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    succeeded_at = models.DateTimeField(blank=True, null=True)

    # Stripe Connect routing (added by 20260520_create_clinics.sql).
    # `stripe_connected_account_id` is snapshotted at create time so a refund
    # or audit can see which account the money was destined for, even if the
    # clinic later re-onboards onto a new account. `transfer_id` is filled in
    # by the `payment_intent.succeeded` webhook — required for refund clawback.
    clinic_id = models.UUIDField(blank=True, null=True)
    stripe_connected_account_id = models.TextField(blank=True, null=True)
    transfer_id = models.TextField(blank=True, null=True)

    # Stripe terminal states. Once an intent is in one of these,
    # we cannot reuse it — we must create a fresh PaymentIntent.
    TERMINAL_STATUSES = frozenset({"succeeded", "canceled"})

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    class Meta:
        managed = False
        db_table = "doc_payment_intents"


class StripeWebhookEvent(models.Model):
    """Mirror of `public.doc_stripe_webhook_events` — webhook idempotency log."""

    id = models.UUIDField(primary_key=True)
    stripe_event_id = models.TextField(unique=True)
    event_type = models.TextField()
    livemode = models.BooleanField(default=False)
    payload = models.JSONField()
    received_at = models.DateTimeField()
    processed_at = models.DateTimeField(blank=True, null=True)
    processing_error = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "doc_stripe_webhook_events"
