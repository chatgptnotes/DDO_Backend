"""
Tests for the Stripe Connect service — onboarding-status derivation and
caller -> clinic resolution. Pure / mock-based: no Postgres, no Stripe key.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from payments.services.connect_service import (
    ClinicNotScoped,
    _derive_onboarding_status,
    resolve_clinic_id,
)


# ---- _derive_onboarding_status --------------------------------------------

def test_status_pending_before_details_submitted():
    account = {"charges_enabled": False, "payouts_enabled": False, "details_submitted": False}
    assert _derive_onboarding_status(account) == "pending"


def test_status_restricted_when_submitted_but_not_chargeable():
    account = {"charges_enabled": False, "payouts_enabled": False, "details_submitted": True}
    assert _derive_onboarding_status(account) == "restricted"


def test_status_active_once_charges_enabled():
    account = {"charges_enabled": True, "payouts_enabled": True, "details_submitted": True}
    assert _derive_onboarding_status(account) == "active"


def test_status_active_even_if_payouts_lag():
    # charges_enabled is the operative capability for taking money.
    account = {"charges_enabled": True, "payouts_enabled": False, "details_submitted": True}
    assert _derive_onboarding_status(account) == "active"


def test_status_disabled_overrides_everything():
    account = {
        "charges_enabled": True,
        "payouts_enabled": True,
        "details_submitted": True,
        "requirements": {"disabled_reason": "rejected.fraud"},
    }
    assert _derive_onboarding_status(account) == "disabled"


# ---- resolve_clinic_id -----------------------------------------------------

def _request(user_id="u1", data=None, query=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        data=data or {},
        query_params=query or {},
    )


def test_clinical_admin_resolves_to_their_scoped_clinic():
    with patch("payments.services.connect_service.has_role", return_value=False), patch(
        "payments.services.connect_service.clinic_scope_id", return_value="clinic-A"
    ):
        assert resolve_clinic_id(_request()) == "clinic-A"


def test_clinical_admin_cannot_name_another_clinic():
    # Even passing clinic_id=clinic-B, a non-superadmin gets their own scope.
    with patch("payments.services.connect_service.has_role", return_value=False), patch(
        "payments.services.connect_service.clinic_scope_id", return_value="clinic-A"
    ):
        assert resolve_clinic_id(_request(data={"clinic_id": "clinic-B"})) == "clinic-A"


def test_superadmin_may_target_an_explicit_clinic():
    with patch("payments.services.connect_service.has_role", return_value=True), patch(
        "payments.services.connect_service.clinic_scope_id", return_value=None
    ):
        assert resolve_clinic_id(_request(data={"clinic_id": "clinic-X"})) == "clinic-X"


def test_unscoped_admin_raises():
    with patch("payments.services.connect_service.has_role", return_value=False), patch(
        "payments.services.connect_service.clinic_scope_id", return_value=None
    ):
        with pytest.raises(ClinicNotScoped):
            resolve_clinic_id(_request())
