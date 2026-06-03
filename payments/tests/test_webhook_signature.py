"""
Tests for Stripe webhook signature verification + idempotency dispatch.

These tests mock both the Stripe SDK and the DB layer, so they do not need a
real Postgres or a real Stripe key.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from payments.services.webhook_handler import (
    WebhookSignatureError,
    handle_webhook,
)


@pytest.fixture
def fake_event():
    return {
        "id": "evt_test_001",
        "type": "payment_intent.succeeded",
        "livemode": False,
        "data": {
            "object": {
                "id": "pi_test_001",
                "amount": 150000,
                "currency": "inr",
                "status": "succeeded",
                "metadata": {"appointment_id": "00000000-0000-0000-0000-000000000001"},
            }
        },
    }


@override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
def test_missing_signature_header_raises():
    with pytest.raises(WebhookSignatureError):
        handle_webhook(raw_body=b"{}", signature_header=None)


@override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
def test_invalid_signature_raises():
    with patch(
        "payments.services.webhook_handler.get_stripe"
    ) as get_stripe_mock:
        # Build a stripe-like module object whose Webhook.construct_event
        # raises SignatureVerificationError, and whose `error.SignatureVerificationError`
        # references the same exception class so the `except` clause matches.
        class FakeSigErr(Exception):
            pass

        stripe_mod = MagicMock()
        stripe_mod.error.SignatureVerificationError = FakeSigErr
        stripe_mod.Webhook.construct_event.side_effect = FakeSigErr("bad sig")
        get_stripe_mock.return_value = stripe_mod

        with pytest.raises(WebhookSignatureError):
            handle_webhook(raw_body=b"{}", signature_header="t=1,v1=bad")


@override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
def test_processed_event_short_circuits(fake_event):
    """A redelivery of an already-PROCESSED event must not dispatch again."""
    with patch("payments.services.webhook_handler.get_stripe") as get_stripe_mock, patch(
        "payments.services.webhook_handler._record_event"
    ), patch(
        "payments.services.webhook_handler._already_processed"
    ) as processed_mock, patch(
        "payments.services.webhook_handler._dispatch"
    ) as dispatch_mock:
        stripe_mod = MagicMock()
        stripe_mod.error.SignatureVerificationError = type("X", (Exception,), {})
        stripe_mod.Webhook.construct_event.return_value = fake_event
        get_stripe_mock.return_value = stripe_mod
        processed_mock.return_value = True  # a prior delivery finished

        result = handle_webhook(raw_body=b"{}", signature_header="t=1,v1=ok")

    assert result.duplicate is True
    dispatch_mock.assert_not_called()


@override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
def test_unprocessed_redelivery_is_reprocessed(fake_event):
    """Reliability R1: a redelivery whose processed_at is still NULL (an earlier
    delivery failed mid-dispatch) must be REPROCESSED, not skipped."""
    with patch("payments.services.webhook_handler.get_stripe") as get_stripe_mock, patch(
        "payments.services.webhook_handler._record_event"
    ), patch(
        "payments.services.webhook_handler._already_processed"
    ) as processed_mock, patch(
        "payments.services.webhook_handler._dispatch"
    ) as dispatch_mock, patch(
        "payments.services.webhook_handler._mark_event_processed"
    ) as mark_processed_mock:
        stripe_mod = MagicMock()
        stripe_mod.error.SignatureVerificationError = type("X", (Exception,), {})
        stripe_mod.Webhook.construct_event.return_value = fake_event
        get_stripe_mock.return_value = stripe_mod
        processed_mock.return_value = False  # row exists but never finished

        result = handle_webhook(raw_body=b"{}", signature_header="t=1,v1=ok")

    assert result.duplicate is False
    dispatch_mock.assert_called_once()
    mark_processed_mock.assert_called_once_with(event_id="evt_test_001")


@override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
def test_first_event_is_dispatched_and_marked_processed(fake_event):
    with patch("payments.services.webhook_handler.get_stripe") as get_stripe_mock, patch(
        "payments.services.webhook_handler._record_event"
    ), patch(
        "payments.services.webhook_handler._already_processed"
    ) as processed_mock, patch(
        "payments.services.webhook_handler._dispatch"
    ) as dispatch_mock, patch(
        "payments.services.webhook_handler._mark_event_processed"
    ) as mark_processed_mock:
        stripe_mod = MagicMock()
        stripe_mod.error.SignatureVerificationError = type("X", (Exception,), {})
        stripe_mod.Webhook.construct_event.return_value = fake_event
        get_stripe_mock.return_value = stripe_mod
        processed_mock.return_value = False  # first time

        result = handle_webhook(raw_body=b"{}", signature_header="t=1,v1=ok")

    assert result.duplicate is False
    dispatch_mock.assert_called_once()
    mark_processed_mock.assert_called_once_with(event_id="evt_test_001")


@override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
def test_handler_failure_marks_error_and_reraises(fake_event):
    with patch("payments.services.webhook_handler.get_stripe") as get_stripe_mock, patch(
        "payments.services.webhook_handler._record_event"
    ), patch(
        "payments.services.webhook_handler._already_processed"
    ) as processed_mock, patch(
        "payments.services.webhook_handler._dispatch"
    ) as dispatch_mock, patch(
        "payments.services.webhook_handler._mark_event_failed"
    ) as mark_failed_mock:
        stripe_mod = MagicMock()
        stripe_mod.error.SignatureVerificationError = type("X", (Exception,), {})
        stripe_mod.Webhook.construct_event.return_value = fake_event
        get_stripe_mock.return_value = stripe_mod
        processed_mock.return_value = False
        dispatch_mock.side_effect = RuntimeError("downstream boom")

        with pytest.raises(RuntimeError, match="downstream boom"):
            handle_webhook(raw_body=b"{}", signature_header="t=1,v1=ok")

    mark_failed_mock.assert_called_once()


def test_missing_secret_raises():
    with override_settings(STRIPE_WEBHOOK_SECRET=""):
        with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
            handle_webhook(raw_body=b"{}", signature_header="t=1,v1=ok")
