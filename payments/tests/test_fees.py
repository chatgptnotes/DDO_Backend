"""Pure unit tests for the server-side fee resolver.

Mirrors test cases from `aidoccall.com/src/utils/currency.js#getDoctorFee` so a
drift between frontend and backend rate / fallback logic shows up immediately.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from payments.services.fees import INR_TO_USD_RATE, resolve_doctor_fee


def test_indian_resident_uses_consultation_fee_inr():
    fee = resolve_doctor_fee(
        {"consultation_fee_inr": 1500, "consultation_fee_usd": 25},
        is_indian_resident=True,
        visit_type="physical",
    )
    assert fee.currency == "inr"
    assert fee.amount_cents == 150_000
    assert fee.is_converted is False


def test_indian_resident_falls_back_to_legacy_consultation_fee():
    fee = resolve_doctor_fee(
        {"consultation_fee": 800},
        is_indian_resident=True,
        visit_type="physical",
    )
    assert fee.currency == "inr"
    assert fee.amount_cents == 80_000


def test_online_visit_picks_online_fee():
    fee = resolve_doctor_fee(
        {"online_fee_inr": 500, "consultation_fee_inr": 1500},
        is_indian_resident=True,
        visit_type="online",
    )
    assert fee.amount_cents == 50_000


def test_intl_patient_uses_doctor_usd_fee_when_set():
    fee = resolve_doctor_fee(
        {"consultation_fee_inr": 1500, "consultation_fee_usd": Decimal("19.99")},
        is_indian_resident=False,
        visit_type="physical",
    )
    assert fee.currency == "usd"
    assert fee.amount_cents == 1999
    assert fee.is_converted is False


def test_intl_patient_falls_back_to_inr_to_usd_conversion():
    fee = resolve_doctor_fee(
        {"consultation_fee_inr": 1500},
        is_indian_resident=False,
        visit_type="physical",
    )
    assert fee.currency == "usd"
    # 1500 INR * 0.012 = 18.00 USD -> 1800 cents
    expected = int((Decimal(1500) * INR_TO_USD_RATE * 100).to_integral_value())
    assert fee.amount_cents == expected
    assert fee.is_converted is True


def test_zero_fee_raises():
    with pytest.raises(ValueError):
        resolve_doctor_fee(
            {"consultation_fee_inr": 0},
            is_indian_resident=True,
            visit_type="physical",
        )


def test_missing_doctor_fields_raises():
    with pytest.raises(ValueError):
        resolve_doctor_fee({}, is_indian_resident=True, visit_type="physical")
