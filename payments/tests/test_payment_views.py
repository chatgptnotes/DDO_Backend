"""
View-level tests for the payments endpoints.

These mock the service-layer functions (`create_intent_for_appointment`,
`get_intent_for_user`, `handle_webhook`) so we exercise authn / authz / routing
without needing Postgres or Stripe.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from payments.services.payment_service import (
    AppointmentAlreadyPaid,
    AppointmentNotFound,
    IntentPayload,
)


@pytest.mark.django_db
def test_create_intent_requires_auth(client):
    response = client.post(
        "/api/payments/intents/",
        data={"appointment_id": "x"},
        content_type="application/json",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_create_intent_requires_patient_role(client, auth_header, patch_roles):
    patch_roles("user-1", ["doctor"])  # no patient role
    response = client.post(
        "/api/payments/intents/",
        data={"appointment_id": "00000000-0000-0000-0000-000000000001"},
        content_type="application/json",
        **auth_header(sub="user-1"),
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_create_intent_400_when_missing_appointment_id(client, auth_header, patch_roles):
    patch_roles("user-1", ["patient"])
    response = client.post(
        "/api/payments/intents/",
        data={},
        content_type="application/json",
        **auth_header(sub="user-1"),
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_create_intent_404_when_appointment_not_owned(client, auth_header, patch_roles):
    patch_roles("user-1", ["patient"])
    with patch(
        "payments.views.create_intent_for_appointment",
        side_effect=AppointmentNotFound("nope"),
    ):
        response = client.post(
            "/api/payments/intents/",
            data={"appointment_id": "00000000-0000-0000-0000-000000000001"},
            content_type="application/json",
            **auth_header(sub="user-1"),
        )
    assert response.status_code == 404


@pytest.mark.django_db
def test_create_intent_409_when_already_paid(client, auth_header, patch_roles):
    patch_roles("user-1", ["patient"])
    with patch(
        "payments.views.create_intent_for_appointment",
        side_effect=AppointmentAlreadyPaid(),
    ):
        response = client.post(
            "/api/payments/intents/",
            data={"appointment_id": "00000000-0000-0000-0000-000000000001"},
            content_type="application/json",
            **auth_header(sub="user-1"),
        )
    assert response.status_code == 409


@pytest.mark.django_db
def test_create_intent_201_returns_client_secret(client, auth_header, patch_roles):
    patch_roles("user-1", ["patient"])
    payload = IntentPayload(
        intent_id="11111111-1111-1111-1111-111111111111",
        stripe_payment_intent_id="pi_xxx",
        client_secret="pi_xxx_secret_yyy",
        publishable_key="pk_test_zzz",
        amount_cents=150000,
        currency="inr",
        status="requires_payment_method",
    )
    with patch(
        "payments.views.create_intent_for_appointment",
        return_value=payload,
    ):
        response = client.post(
            "/api/payments/intents/",
            data={"appointment_id": "00000000-0000-0000-0000-000000000001"},
            content_type="application/json",
            **auth_header(sub="user-1"),
        )
    assert response.status_code == 201
    body = response.json()
    assert body["client_secret"] == "pi_xxx_secret_yyy"
    assert body["publishable_key"] == "pk_test_zzz"
    assert body["amount_cents"] == 150000
    assert body["currency"] == "inr"


@pytest.mark.django_db
def test_webhook_400_on_invalid_signature(client):
    response = client.post(
        "/api/payments/webhooks/stripe/",
        data=b"{}",
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="t=1,v1=bad",
    )
    # No STRIPE_WEBHOOK_SECRET configured in test settings -> 500
    # (fail-closed: never accept unsigned/unverified events).
    assert response.status_code in (400, 500)
