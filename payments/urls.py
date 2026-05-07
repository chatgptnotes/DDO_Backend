"""URL routes for the Stripe payments app."""
from django.urls import path

from .views import (
    CreatePaymentIntentView,
    PaymentIntentStatusView,
    StripeWebhookView,
)

urlpatterns = [
    path(
        "intents/",
        CreatePaymentIntentView.as_view(),
        name="payments-create-intent",
    ),
    path(
        "intents/<uuid:intent_id>/",
        PaymentIntentStatusView.as_view(),
        name="payments-intent-status",
    ),
    path(
        "webhooks/stripe/",
        StripeWebhookView.as_view(),
        name="payments-stripe-webhook",
    ),
]
