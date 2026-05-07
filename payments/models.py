"""
Read-only Django models for the Stripe payments tables.

`managed = False` everywhere — Supabase owns the schema. The CREATE TABLE
statements live in `aidoccall.com/supabase/migrations/20260504_stripe_payments.sql`.

We use raw UUIDFields rather than ForeignKey so:
  * we don't fight Django's relational machinery on `managed=False` tables, and
  * serializers can output `appointment_id`, `patient_id` exactly as PostgREST does.
"""
from __future__ import annotations

from django.db import models


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
