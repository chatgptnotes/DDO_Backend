"""
`reconcile_stripe` — the webhook safety net.

Webhooks can be missed (endpoint downtime), delayed, dropped, or delivered out
of order. Because fulfillment and onboarding state both depend on webhooks, a
single missed event means a patient was charged but their appointment is never
marked paid, or a clinic is stuck `pending` while actually `active`.

This command re-derives both from Stripe — the authoritative source — so the
system is eventually correct even if every webhook for an event is lost. Run it
on a cron every ~15-30 min; it is cheap at ~40 clinics.

    python manage.py reconcile_stripe
    python manage.py reconcile_stripe --payments-window-hours 48
    python manage.py reconcile_stripe --accounts-only

Idempotent — it reuses the same idempotent state-sync functions the webhook
handlers use, so running it concurrently with live webhooks is safe.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from payments.services.connect_service import sync_account_from_stripe
from payments.services.stripe_client import get_stripe
from payments.services.webhook_handler import apply_payment_intent_state

logger = logging.getLogger(__name__)

_NON_TERMINAL_STATUSES = (
    "requires_payment_method",
    "requires_confirmation",
    "requires_action",
    "processing",
    "requires_capture",
)


class Command(BaseCommand):
    help = "Reconcile clinic connected accounts and payment intents against Stripe."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--payments-window-hours",
            type=int,
            default=72,
            help="Reconcile non-terminal intents created within this window (default 72).",
        )
        parser.add_argument(
            "--accounts-only",
            action="store_true",
            help="Reconcile clinic connected accounts only.",
        )
        parser.add_argument(
            "--payments-only",
            action="store_true",
            help="Reconcile payment intents only.",
        )

    def handle(self, *args, **opts) -> None:
        do_accounts = not opts["payments_only"]
        do_payments = not opts["accounts_only"]

        if do_accounts:
            self._reconcile_accounts()
        if do_payments:
            self._reconcile_payments(window_hours=opts["payments_window_hours"])

    # -- accounts ------------------------------------------------------------

    def _reconcile_accounts(self) -> None:
        stripe = get_stripe()
        with connection.cursor() as cur:
            cur.execute(
                "SELECT id::text, stripe_account_id FROM public.clinics "
                "WHERE stripe_account_id IS NOT NULL"
            )
            rows = cur.fetchall()

        synced = failed = 0
        for clinic_id, account_id in rows:
            try:
                account = stripe.Account.retrieve(account_id)
                sync_account_from_stripe(account)
                synced += 1
            except Exception as exc:  # noqa: BLE001 — one bad account must not stop the sweep
                failed += 1
                logger.warning(
                    "reconcile_stripe: account %s (clinic %s) failed: %s",
                    account_id,
                    clinic_id,
                    exc,
                )
        self.stdout.write(
            f"accounts: {synced} synced, {failed} failed, {len(rows)} total"
        )

    # -- payments ------------------------------------------------------------

    def _reconcile_payments(self, *, window_hours: int) -> None:
        cutoff = timezone.now() - timedelta(hours=window_hours)
        stripe = get_stripe()

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT stripe_payment_intent_id, status
                FROM public.doc_payment_intents
                WHERE status = ANY(%s)
                  AND created_at >= %s
                ORDER BY created_at
                """,
                [list(_NON_TERMINAL_STATUSES), cutoff],
            )
            rows = cur.fetchall()

        recovered = updated = failed = 0
        for pi_id, local_status in rows:
            try:
                intent = stripe.PaymentIntent.retrieve(pi_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("reconcile_stripe: intent %s failed: %s", pi_id, exc)
                continue

            if intent.get("status") == local_status:
                continue  # already in sync — nothing to do

            # apply_payment_intent_state is idempotent and runs fulfillment for
            # a succeeded intent, recovering a missed payment_intent.succeeded.
            apply_payment_intent_state(intent)
            updated += 1
            if intent.get("status") == "succeeded":
                recovered += 1
                logger.info(
                    "reconcile_stripe: recovered missed success for intent %s", pi_id
                )

        self.stdout.write(
            f"payments: {updated} updated ({recovered} recovered as paid), "
            f"{failed} failed, {len(rows)} checked"
        )
