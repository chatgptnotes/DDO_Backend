"""
Stripe Connect — Express connected-account lifecycle for clinics.

Each clinic owns one Express connected account. Patient payments route to that
account via destination charges (see `payment_service.py`); this module owns
everything *before* the money path — creating the account, generating
Stripe-hosted onboarding links, and keeping the clinic's denormalised
capability mirror (`charges_enabled` etc.) in sync with Stripe.

Source-of-truth rule (mirrors the rest of the payments app): a connected
account's capabilities are authoritative *only* from Stripe — the
`account.updated` webhook and the `reconcile_stripe` sweep both call
`sync_account_from_stripe()` with a freshly retrieved Account.

`Account.create` is a dual-write (Stripe mints `acct_…`, then we persist it).
`create_connected_account()` makes that safe with a `SELECT … FOR UPDATE` row
lock plus a Stripe idempotency key, so a retry never mints a second account.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from django.conf import settings
from django.db import connection, transaction

from core.roles import clinic_scope_id, has_role

from .stripe_client import get_stripe

logger = logging.getLogger(__name__)

# MCC 8011 = "Doctors and Physicians". Used as the connected account's default
# business category; the clinic can refine it during onboarding.
_CLINIC_MCC = "8011"

# Hold patient funds in the clinic's Stripe balance for a week before paying
# out, so a `reverse_transfer` refund shortly after a consultation does not hit
# an already-emptied balance (reliability item R9).
_PAYOUT_DELAY_DAYS = 7


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConnectError(Exception):
    """Any expected, user-visible Connect failure."""


class ClinicNotFound(ConnectError):
    pass


class ClinicNotScoped(ConnectError):
    """Caller is not authorised to act on the target clinic."""


class OnboardingNotReady(ConnectError):
    """An action requires onboarding progress the clinic has not made."""


@dataclass(frozen=True)
class OnboardingStatus:
    clinic_id: str
    name: str
    has_account: bool
    onboarding_status: str
    charges_enabled: bool
    payouts_enabled: bool
    details_submitted: bool
    requirements_due: dict
    default_currency: str | None
    country: str | None


# ---------------------------------------------------------------------------
# Stripe Account -> clinic state mapping
# ---------------------------------------------------------------------------

def _derive_onboarding_status(account: Any) -> str:
    """Collapse a Stripe Account's capability flags into our status enum.

    `charges_enabled` is the operative capability — it is what lets the clinic
    receive a destination charge — so it drives `active`. `payouts_enabled` is
    surfaced separately as a warning (a clinic can bank funds in Stripe even
    while payouts are briefly held).
    """
    requirements = account.get("requirements") or {}
    if requirements.get("disabled_reason"):
        return "disabled"
    if account.get("charges_enabled"):
        return "active"
    if account.get("details_submitted"):
        return "restricted"  # submitted, but Stripe wants more / is reviewing
    return "pending"


def sync_account_from_stripe(
    account: Any, *, event_created_at: datetime | None = None
) -> bool:
    """Apply a freshly retrieved Stripe Account onto its clinic row.

    `event_created_at` is the `account.updated` event's `created` time when this
    sync is webhook-driven; it gates the write so a stale, out-of-order event
    cannot regress a clinic (reliability item R3). Pass None for reconcile /
    status syncs — a direct `Account.retrieve` is always current truth.

    Returns True if a clinic row was updated; False if no clinic matched the
    account, or the event was older than the last applied update.
    """
    account_id = account.get("id")
    if not account_id:
        return False

    status = _derive_onboarding_status(account)
    applied_at = event_created_at or datetime.now(timezone.utc)
    requirements = account.get("requirements") or {}

    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE public.clinics
               SET charges_enabled    = %s,
                   payouts_enabled    = %s,
                   details_submitted  = %s,
                   requirements_due   = %s::jsonb,
                   default_currency   = %s,
                   onboarding_status  = %s,
                   stripe_livemode    = %s,
                   account_updated_at = %s
             WHERE stripe_account_id  = %s
               AND (%s IS NULL
                    OR account_updated_at IS NULL
                    OR account_updated_at <= %s)
            RETURNING id::text
            """,
            [
                bool(account.get("charges_enabled")),
                bool(account.get("payouts_enabled")),
                bool(account.get("details_submitted")),
                json.dumps(requirements),
                account.get("default_currency"),
                status,
                bool(account.get("livemode")),
                applied_at,
                account_id,
                event_created_at,
                event_created_at,
            ],
        )
        row = cur.fetchone()

    if row is None:
        logger.warning(
            "sync_account_from_stripe: no clinic for account %s "
            "(unknown account or stale event)",
            account_id,
        )
        return False

    logger.info("Clinic %s synced from account %s -> %s", row[0], account_id, status)
    return True


# ---------------------------------------------------------------------------
# Account creation
# ---------------------------------------------------------------------------

def create_connected_account(
    *, clinic_id: str, country: str | None = None, email: str | None = None
) -> str:
    """Create (or return the existing) Express connected account for a clinic.

    Idempotent and concurrency-safe: the clinic row is locked `FOR UPDATE`, so a
    second caller — or a retry after a lost response — either returns the
    account the first call persisted, or is serialised behind it. The Stripe
    `idempotency_key` covers the remaining window where Stripe minted the
    account but our write was lost.

    `country` (ISO-3166-1 alpha-2) is required if the clinic has none stored;
    it is immutable once the account exists.
    """
    stripe = get_stripe()

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT name, contact_email, country, stripe_account_id
                FROM public.clinics
                WHERE id = %s
                FOR UPDATE
                """,
                [clinic_id],
            )
            row = cur.fetchone()
            if row is None:
                raise ClinicNotFound(f"No clinic with id {clinic_id}")

            name, contact_email, clinic_country, existing_account = row
            if existing_account:
                return existing_account  # already onboarded — idempotent

            resolved_country = (country or clinic_country or "").upper()
            if not resolved_country:
                raise ConnectError(
                    "Clinic country is required to create a Stripe account."
                )
            resolved_email = email or contact_email or None

            account = stripe.Account.create(
                type="express",
                country=resolved_country,
                email=resolved_email,
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
                business_profile={"name": name, "mcc": _CLINIC_MCC},
                settings={
                    "payouts": {
                        "schedule": {
                            "interval": "daily",
                            "delay_days": _PAYOUT_DELAY_DAYS,
                        }
                    }
                },
                metadata={"clinic_id": str(clinic_id)},
                idempotency_key=f"connect_acct_{clinic_id}",
            )
            logger.info(
                "Created Express account %s for clinic %s", account.id, clinic_id
            )

            cur.execute(
                """
                UPDATE public.clinics
                   SET stripe_account_id  = %s,
                       stripe_livemode    = %s,
                       country            = %s,
                       contact_email      = COALESCE(contact_email, %s),
                       onboarding_status  = %s,
                       charges_enabled    = %s,
                       payouts_enabled    = %s,
                       details_submitted  = %s,
                       account_updated_at = NOW()
                 WHERE id = %s
                """,
                [
                    account.id,
                    bool(account.get("livemode")),
                    resolved_country,
                    resolved_email,
                    _derive_onboarding_status(account),
                    bool(account.get("charges_enabled")),
                    bool(account.get("payouts_enabled")),
                    bool(account.get("details_submitted")),
                    clinic_id,
                ],
            )
            return account.id


def create_onboarding_link(*, clinic_id: str) -> str:
    """Return a fresh, single-use Stripe-hosted onboarding URL for the clinic.

    Account Links expire within minutes — always mint a new one on demand. The
    `refresh_url` points back at the clinic-admin Payments page, which simply
    re-requests this endpoint, making an abandoned/expired link resumable.
    """
    account_id = _require_account_id(clinic_id)
    stripe = get_stripe()
    link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=settings.CONNECT_ONBOARDING_REFRESH_URL,
        return_url=settings.CONNECT_ONBOARDING_RETURN_URL,
        type="account_onboarding",
    )
    return link.url


def create_dashboard_login_link(*, clinic_id: str) -> str:
    """Return an Express dashboard login link so the clinic can view payouts.

    Only valid once the clinic has submitted onboarding details.
    """
    clinic = _load_clinic(clinic_id)
    if clinic is None:
        raise ClinicNotFound(f"No clinic with id {clinic_id}")
    if not clinic["stripe_account_id"] or not clinic["details_submitted"]:
        raise OnboardingNotReady(
            "The clinic must finish Stripe onboarding before opening the dashboard."
        )
    stripe = get_stripe()
    link = stripe.Account.create_login_link(clinic["stripe_account_id"])
    return link.url


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def get_onboarding_status(*, clinic_id: str, live_sync: bool = True) -> OnboardingStatus:
    """Return the clinic's onboarding state.

    When `live_sync` is set and the clinic has an account, this first does a
    live `Account.retrieve` and reconciles the row — so the status is correct
    even if the `account.updated` webhook was missed or delayed.
    """
    clinic = _load_clinic(clinic_id)
    if clinic is None:
        raise ClinicNotFound(f"No clinic with id {clinic_id}")

    if live_sync and clinic["stripe_account_id"]:
        try:
            account = get_stripe().Account.retrieve(clinic["stripe_account_id"])
            sync_account_from_stripe(account)
            clinic = _load_clinic(clinic_id) or clinic
        except Exception as exc:  # noqa: BLE001 — status must still render
            logger.warning(
                "Live account sync failed for clinic %s: %s", clinic_id, exc
            )

    return OnboardingStatus(
        clinic_id=clinic["id"],
        name=clinic["name"],
        has_account=bool(clinic["stripe_account_id"]),
        onboarding_status=clinic["onboarding_status"],
        charges_enabled=clinic["charges_enabled"],
        payouts_enabled=clinic["payouts_enabled"],
        details_submitted=clinic["details_submitted"],
        requirements_due=clinic["requirements_due"] or {},
        default_currency=clinic["default_currency"],
        country=clinic["country"],
    )


def list_clinics_for_superadmin() -> list[dict[str, Any]]:
    """All clinics with their Connect state — the superadmin operations view."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT c.id::text,
                   c.name,
                   c.country,
                   c.stripe_account_id,
                   c.onboarding_status,
                   c.charges_enabled,
                   c.payouts_enabled,
                   c.details_submitted,
                   c.requirements_due,
                   c.default_currency,
                   c.account_updated_at,
                   (SELECT COUNT(*) FROM public.doc_doctors d WHERE d.clinic_id = c.id)
            FROM public.clinics c
            ORDER BY c.name
            """
        )
        rows = cur.fetchall()

    return [
        {
            "clinic_id": r[0],
            "name": r[1],
            "country": r[2],
            "has_account": bool(r[3]),
            "onboarding_status": r[4],
            "charges_enabled": r[5],
            "payouts_enabled": r[6],
            "details_submitted": r[7],
            "requirements_due": r[8] or {},
            "default_currency": r[9],
            "account_updated_at": r[10].isoformat() if r[10] else None,
            "doctor_count": r[11],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Caller -> clinic resolution
# ---------------------------------------------------------------------------

def resolve_clinic_id(request) -> str:
    """Resolve which clinic the caller is acting on.

    A `clinical_admin` always acts on the single clinic they are scoped to
    (`user_roles.scope_id`) — they cannot name another. A `superadmin` may
    target any clinic by passing `clinic_id` in the request, and falls back to
    their own scope if they omit it.
    """
    user_id = request.user.id
    explicit = (
        (getattr(request, "data", None) or {}).get("clinic_id")
        or request.query_params.get("clinic_id")
    )

    if has_role(user_id, "superadmin") and explicit:
        return str(explicit)

    scoped = clinic_scope_id(user_id)
    if scoped:
        return scoped
    if explicit and has_role(user_id, "superadmin"):
        return str(explicit)

    raise ClinicNotScoped(
        "No clinic is associated with your account. "
        "Ask a superadmin to link your clinical-admin role to a clinic."
    )


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------

def _load_clinic(clinic_id: str) -> dict[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, name, contact_email, country,
                   stripe_account_id, onboarding_status,
                   charges_enabled, payouts_enabled, details_submitted,
                   requirements_due, default_currency
            FROM public.clinics
            WHERE id = %s
            LIMIT 1
            """,
            [clinic_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        keys = (
            "id",
            "name",
            "contact_email",
            "country",
            "stripe_account_id",
            "onboarding_status",
            "charges_enabled",
            "payouts_enabled",
            "details_submitted",
            "requirements_due",
            "default_currency",
        )
        return dict(zip(keys, row))


def _require_account_id(clinic_id: str) -> str:
    clinic = _load_clinic(clinic_id)
    if clinic is None:
        raise ClinicNotFound(f"No clinic with id {clinic_id}")
    if not clinic["stripe_account_id"]:
        raise OnboardingNotReady(
            "Create the clinic's Stripe account before requesting an onboarding link."
        )
    return clinic["stripe_account_id"]
