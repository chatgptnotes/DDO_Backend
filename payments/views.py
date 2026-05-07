"""
HTTP entrypoints for the Stripe payments flow.

Endpoints
---------
POST /api/payments/intents/                  Create or reuse a PaymentIntent
                                              for the requester's appointment.
GET  /api/payments/intents/{intent_id}/       Fetch owner-scoped intent state.
POST /api/payments/webhooks/stripe/           Stripe webhook receiver
                                              (signature-verified, no auth).
"""
from __future__ import annotations

import logging

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRole

from .permissions import IsIntentOwner
from .services.payment_service import (
    AppointmentAlreadyPaid,
    AppointmentNotFound,
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


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(APIView):
    """POST /api/payments/webhooks/stripe/ — signature-verified Stripe receiver."""

    # No DRF auth — we authenticate via the Stripe-Signature header.
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request) -> Response:
        signature = request.META.get("HTTP_STRIPE_SIGNATURE")
        try:
            result = handle_webhook(
                raw_body=request.body,
                signature_header=signature,
            )
        except WebhookSignatureError as exc:
            logger.warning("Rejected Stripe webhook: %s", exc)
            return Response(
                {"detail": "Invalid signature."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:  # noqa: BLE001 — surface 500 so Stripe retries
            logger.exception("Webhook processing failed: %s", exc)
            return Response(
                {"detail": "Webhook processing error."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"received": True, "duplicate": result.duplicate},
            status=status.HTTP_200_OK,
        )
