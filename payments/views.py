"""
HTTP entrypoints for the Stripe payments flow.

Endpoints
---------
POST /api/payments/intents/                   Create or reuse a PaymentIntent
                                               for the requester's appointment.
GET  /api/payments/intents/{intent_id}/        Fetch owner-scoped intent state.
POST /api/payments/webhooks/stripe/            Platform-account webhook receiver.
POST /api/payments/webhooks/stripe/connect/    Connected-account webhook receiver.

Stripe Connect onboarding (clinical_admin / superadmin)
-------------------------------------------------------
POST /api/payments/connect/accounts/           Create the clinic's Express account.
POST /api/payments/connect/account-links/      Fresh, resumable onboarding link.
GET  /api/payments/connect/status/             Clinic onboarding state (live-synced).
POST /api/payments/connect/dashboard-link/     Express dashboard login link.
GET  /api/payments/connect/clinics/            All clinics' state (superadmin only).
"""
from __future__ import annotations

import logging
from dataclasses import asdict

import stripe
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRole

from .permissions import IsClinicScopedAdmin, IsIntentOwner
from .services.connect_service import (
    ClinicNotFound,
    ClinicNotScoped,
    ConnectError,
    OnboardingNotReady,
    create_connected_account,
    create_dashboard_login_link,
    create_onboarding_link,
    get_onboarding_status,
    list_clinics_for_superadmin,
    resolve_clinic_id,
)
from .services.payment_service import (
    AppointmentAlreadyPaid,
    AppointmentNotFound,
    ClinicNotPayable,
    PaymentError,
    create_intent_for_appointment,
)
from .services.webhook_handler import WebhookSignatureError, handle_webhook

logger = logging.getLogger(__name__)


class CreatePaymentIntentView(APIView):
    """POST /api/payments/intents/ — body: {"appointment_id": "<uuid>"}."""

    permission_classes = [IsAuthenticated, HasRole("patient", "user")]

    def post(self, request) -> Response:
        appointment_id = request.data.get("appointment_id")
        if not appointment_id:
            return Response(
                {"detail": "appointment_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            payload = create_intent_for_appointment(
                appointment_id=str(appointment_id),
                requester_user_id=request.user.id,
            )
        except AppointmentNotFound:
            return Response(
                {"detail": "Appointment not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except AppointmentAlreadyPaid:
            return Response(
                {"detail": "Appointment is already paid."},
                status=status.HTTP_409_CONFLICT,
            )
        except ClinicNotPayable as exc:
            # The clinic has not finished Stripe onboarding — fail fast and
            # legibly rather than letting the destination charge error later.
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except PaymentError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "intent_id": payload.intent_id,
                "stripe_payment_intent_id": payload.stripe_payment_intent_id,
                "client_secret": payload.client_secret,
                "publishable_key": payload.publishable_key,
                "amount_cents": payload.amount_cents,
                "currency": payload.currency,
                "status": payload.status,
            },
            status=status.HTTP_201_CREATED,
        )


class PaymentIntentStatusView(APIView):
    """GET /api/payments/intents/{intent_id}/ — owner-scoped status poller."""

    permission_classes = [IsAuthenticated, IsIntentOwner]

    def get(self, request, intent_id: str) -> Response:
        intent = getattr(request, "_payment_intent_record", None)
        if intent is None:
            return Response(
                {"detail": "Not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(intent, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Stripe Connect — onboarding
# ---------------------------------------------------------------------------

def _connect_error_response(exc: Exception) -> Response | None:
    """Map a connect_service exception to an HTTP response, or None if unknown."""
    if isinstance(exc, ClinicNotFound):
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    if isinstance(exc, ClinicNotScoped):
        return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
    if isinstance(exc, OnboardingNotReady):
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    if isinstance(exc, ConnectError):
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if isinstance(exc, stripe.error.StripeError):
        logger.exception("Stripe Connect API error: %s", exc)
        return Response(
            {"detail": "Stripe is temporarily unavailable. Please try again."},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return None


class CreateConnectAccountView(APIView):
    """POST /api/payments/connect/accounts/ — create the clinic's Express account.

    Body (optional): {"country": "IN", "email": "...", "clinic_id": "..."}.
    `clinic_id` is honoured only for superadmins; a clinical_admin always acts
    on their own scoped clinic. Returns the account id plus a fresh onboarding
    URL so the frontend can redirect straight into Stripe-hosted onboarding.
    """

    permission_classes = [IsAuthenticated, IsClinicScopedAdmin]

    def post(self, request) -> Response:
        try:
            clinic_id = resolve_clinic_id(request)
            account_id = create_connected_account(
                clinic_id=clinic_id,
                country=request.data.get("country"),
                email=request.data.get("email"),
            )
            onboarding_url = create_onboarding_link(clinic_id=clinic_id)
        except Exception as exc:  # noqa: BLE001 — mapped below
            mapped = _connect_error_response(exc)
            if mapped is not None:
                return mapped
            raise

        return Response(
            {
                "clinic_id": clinic_id,
                "account_id": account_id,
                "onboarding_url": onboarding_url,
            },
            status=status.HTTP_201_CREATED,
        )


class CreateAccountLinkView(APIView):
    """POST /api/payments/connect/account-links/ — fresh, resumable onboarding link."""

    permission_classes = [IsAuthenticated, IsClinicScopedAdmin]

    def post(self, request) -> Response:
        try:
            clinic_id = resolve_clinic_id(request)
            onboarding_url = create_onboarding_link(clinic_id=clinic_id)
        except Exception as exc:  # noqa: BLE001 — mapped below
            mapped = _connect_error_response(exc)
            if mapped is not None:
                return mapped
            raise
        return Response({"onboarding_url": onboarding_url}, status=status.HTTP_200_OK)


class ConnectStatusView(APIView):
    """GET /api/payments/connect/status/ — clinic onboarding state (live-synced)."""

    permission_classes = [IsAuthenticated, IsClinicScopedAdmin]

    def get(self, request) -> Response:
        try:
            clinic_id = resolve_clinic_id(request)
            state = get_onboarding_status(clinic_id=clinic_id, live_sync=True)
        except Exception as exc:  # noqa: BLE001 — mapped below
            mapped = _connect_error_response(exc)
            if mapped is not None:
                return mapped
            raise
        return Response(asdict(state), status=status.HTTP_200_OK)


class ConnectDashboardLinkView(APIView):
    """POST /api/payments/connect/dashboard-link/ — Express dashboard login link."""

    permission_classes = [IsAuthenticated, IsClinicScopedAdmin]

    def post(self, request) -> Response:
        try:
            clinic_id = resolve_clinic_id(request)
            dashboard_url = create_dashboard_login_link(clinic_id=clinic_id)
        except Exception as exc:  # noqa: BLE001 — mapped below
            mapped = _connect_error_response(exc)
            if mapped is not None:
                return mapped
            raise
        return Response({"dashboard_url": dashboard_url}, status=status.HTTP_200_OK)


class SuperadminClinicListView(APIView):
    """GET /api/payments/connect/clinics/ — every clinic's Connect state.

    The operations view: spot a `restricted` or `disabled` clinic before
    patients hit failed checkouts.
    """

    permission_classes = [IsAuthenticated, HasRole("superadmin")]

    def get(self, request) -> Response:
        return Response(
            {"clinics": list_clinics_for_superadmin()}, status=status.HTTP_200_OK
        )


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

class _StripeWebhookBase(APIView):
    """Shared signature-verified Stripe webhook receiver.

    Auth is the `Stripe-Signature` header, not DRF — so no auth classes and no
    CSRF. `stream` selects the signing secret + handler table.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    stream = "platform"

    def post(self, request) -> Response:
        signature = request.META.get("HTTP_STRIPE_SIGNATURE")
        try:
            result = handle_webhook(
                raw_body=request.body,
                signature_header=signature,
                stream=self.stream,
            )
        except WebhookSignatureError as exc:
            logger.warning("Rejected Stripe %s webhook: %s", self.stream, exc)
            return Response(
                {"detail": "Invalid signature."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:  # noqa: BLE001 — surface 500 so Stripe retries
            logger.exception("Webhook (%s) processing failed: %s", self.stream, exc)
            return Response(
                {"detail": "Webhook processing error."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"received": True, "duplicate": result.duplicate},
            status=status.HTTP_200_OK,
        )


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(_StripeWebhookBase):
    """POST /api/payments/webhooks/stripe/ — platform-account events
    (payment_intent.*, charge.refunded)."""

    stream = "platform"


@method_decorator(csrf_exempt, name="dispatch")
class StripeConnectWebhookView(_StripeWebhookBase):
    """POST /api/payments/webhooks/stripe/connect/ — connected-account events
    (account.updated). Verified with STRIPE_CONNECT_WEBHOOK_SECRET."""

    stream = "connect"
