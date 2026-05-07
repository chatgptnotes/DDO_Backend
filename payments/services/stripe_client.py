"""
Thin wrapper around the official `stripe` Python SDK.

Centralising configuration here keeps `STRIPE_SECRET_KEY` and the API version
out of every callsite, and makes mocking trivial in tests (just patch
`payments.services.stripe_client.get_stripe`).
"""
from __future__ import annotations

import logging

import stripe
from django.conf import settings

logger = logging.getLogger(__name__)

# Pin the API version so a Stripe-side schema change does not silently shift
# our webhook payload shape. Bump deliberately, with regression tests.
_STRIPE_API_VERSION = "2024-06-20"


def get_stripe() -> "stripe":
    """Return the configured `stripe` module (ready to call .PaymentIntent.create etc.)."""
    if not stripe.api_key:
        secret = getattr(settings, "STRIPE_SECRET_KEY", "")
        if not secret:
            raise RuntimeError(
                "STRIPE_SECRET_KEY is not configured. "
                "Set it in the backend environment before using payments."
            )
        stripe.api_key = secret
        stripe.api_version = _STRIPE_API_VERSION
        logger.info("Stripe client configured (api_version=%s)", _STRIPE_API_VERSION)
    return stripe
