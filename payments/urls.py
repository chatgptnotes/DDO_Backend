"""URL routes for the Stripe payments app."""
from django.urls import path

from .views import (
    ConnectDashboardLinkView,
    ConnectStatusView,
    CreateAccountLinkView,
    CreateConnectAccountView,
    CreatePaymentIntentView,
    PaymentIntentStatusView,
    StripeConnectWebhookView,
    StripeWebhookView,
    SuperadminClinicListView,
)

urlpatterns = [
    # ---- Patient payments --------------------------------------------------
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
    # ---- Stripe Connect onboarding (clinical_admin / superadmin) -----------
    path(
        "connect/accounts/",
        CreateConnectAccountView.as_view(),
        name="payments-connect-create-account",
    ),
    path(
        "connect/account-links/",
        CreateAccountLinkView.as_view(),
        name="payments-connect-account-link",
    ),
    path(
        "connect/status/",
        ConnectStatusView.as_view(),
        name="payments-connect-status",
    ),
    path(
        "connect/dashboard-link/",
        ConnectDashboardLinkView.as_view(),
        name="payments-connect-dashboard-link",
    ),
    path(
        "connect/clinics/",
        SuperadminClinicListView.as_view(),
        name="payments-connect-clinic-list",
    ),
    # ---- Webhooks ----------------------------------------------------------
    path(
        "webhooks/stripe/",
        StripeWebhookView.as_view(),
        name="payments-stripe-webhook",
    ),
    path(
        "webhooks/stripe/connect/",
        StripeConnectWebhookView.as_view(),
        name="payments-stripe-connect-webhook",
    ),
]
