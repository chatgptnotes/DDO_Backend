"""
Server-side port of `aidoccall.com/src/utils/currency.js#getDoctorFee`.

The intent amount must be derived server-side from the doctor's profile and
the patient's residency, never from a client-supplied number — otherwise a
manipulated request could pay $1 for a $200 consultation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

logger = logging.getLogger(__name__)

# Fallback rate when a doctor has not configured a USD fee. Mirrors the
# frontend constant exactly. Update both at the same time.
INR_TO_USD_RATE = Decimal("0.012")


@dataclass(frozen=True)
class ResolvedFee:
    """Server-resolved consultation fee.

    `amount_cents` is what we charge in Stripe — minor units, integer.
    `currency` is the ISO-4217 lowercased code Stripe expects.
    """

    amount_cents: int
    currency: str
    is_converted: bool = False


def _coerce_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def resolve_doctor_fee(
    doctor: Mapping,
    *,
    is_indian_resident: bool,
    visit_type: str = "physical",
) -> ResolvedFee:
    """Mirror of `getDoctorFee(doctor, isIndianResident, visitType)`.

    `doctor` is a dict-like mapping with the same fee fields the frontend
    reads:
        consultation_fee_inr, online_fee_inr, consultation_fee, online_fee
        consultation_fee_usd, online_fee_usd
    """
    is_online = visit_type == "online"

    if is_indian_resident:
        if is_online:
            inr = (
                _coerce_decimal(doctor.get("online_fee_inr"))
                or _coerce_decimal(doctor.get("online_fee"))
                or _coerce_decimal(doctor.get("consultation_fee"))
            )
        else:
            inr = (
                _coerce_decimal(doctor.get("consultation_fee_inr"))
                or _coerce_decimal(doctor.get("consultation_fee"))
                or _coerce_decimal(doctor.get("online_fee"))
            )
        return ResolvedFee(
            amount_cents=_to_minor_units(inr, "inr"),
            currency="inr",
        )

    # International patient — prefer doctor-specified USD fee.
    if is_online:
        usd = (
            _coerce_decimal(doctor.get("online_fee_usd"))
            or _coerce_decimal(doctor.get("consultation_fee_usd"))
        )
    else:
        usd = (
            _coerce_decimal(doctor.get("consultation_fee_usd"))
            or _coerce_decimal(doctor.get("online_fee_usd"))
        )

    if usd > 0:
        return ResolvedFee(
            amount_cents=_to_minor_units(usd, "usd"),
            currency="usd",
        )

    # Fallback — convert INR fee to USD with the documented rate.
    if is_online:
        inr = (
            _coerce_decimal(doctor.get("online_fee_inr"))
            or _coerce_decimal(doctor.get("online_fee"))
            or _coerce_decimal(doctor.get("consultation_fee"))
        )
    else:
        inr = (
            _coerce_decimal(doctor.get("consultation_fee_inr"))
            or _coerce_decimal(doctor.get("consultation_fee"))
            or _coerce_decimal(doctor.get("online_fee"))
        )
    converted = (inr * INR_TO_USD_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return ResolvedFee(
        amount_cents=_to_minor_units(converted, "usd"),
        currency="usd",
        is_converted=True,
    )


def _to_minor_units(amount: Decimal, currency: str) -> int:
    """Convert a major-unit Decimal to minor units (cents/paise) as an int.

    Both INR and USD use 2 decimal minor units in Stripe's convention.
    """
    if amount <= 0:
        raise ValueError(f"Fee must be positive, got {amount} {currency}")
    minor = (amount * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(minor)
