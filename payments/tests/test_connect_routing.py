"""
Tests for the Connect payment-routing guard at the view layer:
a clinic that has not finished Stripe onboarding yields HTTP 409.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from payments.services.payment_service import ClinicNotPayable

_APPT = "00000000-0000-0000-0000-000000000001"


@pytest.mark.django_db
def test_create_intent_returns_409_when_clinic_not_payable(client, auth_header, patch_roles):
    """The guard fails fast — before any Stripe call — with a clear 409."""
    patch_roles("user-1", ["patient"])
    with patch(
        "payments.views.create_intent_for_appointment",
        side_effect=ClinicNotPayable("This clinic has not finished payment setup."),
    ):
        response = client.post(
            "/api/payments/intents/",
            data={"appointment_id": _APPT},
            content_type="application/json",
            **auth_header(sub="user-1"),
        )

    assert response.status_code == 409
    assert "payment setup" in response.json()["detail"]


@pytest.mark.django_db
def test_create_intent_still_400s_for_generic_payment_error(client, auth_header, patch_roles):
    """A non-clinic PaymentError keeps the existing 400 mapping."""
    from payments.services.payment_service import PaymentError

    patch_roles("user-1", ["patient"])
    with patch(
        "payments.views.create_intent_for_appointment",
        side_effect=PaymentError("Doctor profile not found for this appointment."),
    ):
        response = client.post(
            "/api/payments/intents/",
            data={"appointment_id": _APPT},
            content_type="application/json",
            **auth_header(sub="user-1"),
        )

    assert response.status_code == 400
