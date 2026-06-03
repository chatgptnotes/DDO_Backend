"""
Tests for the Connect webhook stream and the shared payment-state applier.
Mock-based: no Postgres, no Stripe key.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from payments.services.webhook_handler import apply_payment_intent_state, handle_webhook


# ---- Connect stream secret handling ---------------------------------------

def test_connect_stream_fails_closed_without_its_secret():
    """The Connect endpoint must not fall back to the platform secret."""
    with override_settings(STRIPE_CONNECT_WEBHOOK_SECRET="", STRIPE_WEBHOOK_SECRET="whsec_platform"):
        with pytest.raises(RuntimeError, match="STRIPE_CONNECT_WEBHOOK_SECRET"):
            handle_webhook(raw_body=b"{}", signature_header="t=1,v1=ok", stream="connect")


@override_settings(
    STRIPE_WEBHOOK_SECRET="whsec_platform",
    STRIPE_CONNECT_WEBHOOK_SECRET="whsec_connect",
)
def test_connect_stream_verifies_with_the_connect_secret():
    """Each stream verifies signatures with its own secret — not interchangeable."""
    event = {"id": "evt_acct_1", "type": "account.updated", "livemode": False,
             "data": {"object": {"id": "acct_1"}}, "created": 1700000000}
    with patch("payments.services.webhook_handler.get_stripe") as get_stripe_mock, patch(
        "payments.services.webhook_handler._record_event"
    ), patch(
        "payments.services.webhook_handler._already_processed", return_value=True
    ):
        stripe_mod = MagicMock()
        stripe_mod.error.SignatureVerificationError = type("X", (Exception,), {})
        stripe_mod.Webhook.construct_event.return_value = event
        get_stripe_mock.return_value = stripe_mod

        handle_webhook(raw_body=b"{}", signature_header="t=1,v1=ok", stream="connect")

        _, kwargs = stripe_mod.Webhook.construct_event.call_args
        assert kwargs["secret"] == "whsec_connect"


@override_settings(
    STRIPE_WEBHOOK_SECRET="whsec_platform",
    STRIPE_CONNECT_WEBHOOK_SECRET="whsec_connect",
)
def test_account_updated_routes_to_sync():
    """An account.updated event reaches the connected-account sync handler."""
    event = {"id": "evt_acct_2", "type": "account.updated", "livemode": False,
             "data": {"object": {"id": "acct_2"}}, "created": 1700000000}
    with patch("payments.services.webhook_handler.get_stripe") as get_stripe_mock, patch(
        "payments.services.webhook_handler._record_event"
    ), patch(
        "payments.services.webhook_handler._already_processed", return_value=False
    ), patch(
        "payments.services.webhook_handler._mark_event_processed"
    ), patch(
        "payments.services.webhook_handler.sync_account_from_stripe"
    ) as sync_mock:
        stripe_mod = MagicMock()
        stripe_mod.error.SignatureVerificationError = type("X", (Exception,), {})
        stripe_mod.Webhook.construct_event.return_value = event
        stripe_mod.Account.retrieve.return_value = {"id": "acct_2", "charges_enabled": True}
        get_stripe_mock.return_value = stripe_mod

        result = handle_webhook(
            raw_body=b"{}", signature_header="t=1,v1=ok", stream="connect"
        )

    assert result.duplicate is False
    sync_mock.assert_called_once()
    # The out-of-order guard is fed the event's `created` time.
    assert sync_mock.call_args.kwargs["event_created_at"] is not None


# ---- apply_payment_intent_state (shared by webhook + reconcile) -----------

def test_succeeded_intent_triggers_fulfillment():
    intent = {
        "id": "pi_1",
        "status": "succeeded",
        "amount": 150000,
        "currency": "inr",
        "latest_charge": "ch_1",
        "metadata": {"appointment_id": "appt-1"},
    }
    with patch("payments.services.webhook_handler._update_intent_status") as upd, patch(
        "payments.services.webhook_handler._store_transfer_id"
    ), patch(
        "payments.services.webhook_handler.fulfill_paid_appointment"
    ) as fulfill:
        apply_payment_intent_state(intent)

    upd.assert_called_once()
    assert upd.call_args.kwargs["status"] == "succeeded"
    fulfill.assert_called_once()
    assert fulfill.call_args.kwargs["appointment_id"] == "appt-1"


def test_non_succeeded_intent_does_not_fulfill():
    intent = {"id": "pi_2", "status": "requires_payment_method", "amount": 1, "currency": "inr"}
    with patch("payments.services.webhook_handler._update_intent_status") as upd, patch(
        "payments.services.webhook_handler.fulfill_paid_appointment"
    ) as fulfill:
        apply_payment_intent_state(intent)

    upd.assert_called_once()
    fulfill.assert_not_called()
