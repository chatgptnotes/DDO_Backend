"""
Top-level URL routing.

Layout:
- /api/health/                — public liveness check
- /api/me/                    — authenticated user info
- /api/surgeon/...            — AiSurgeonPilot endpoints
- /api/aidoccall/...          — aidoccall.com endpoints
- /api/payments/...           — Stripe payments (intents + webhook)
"""
from django.urls import include, path

from core.views import HealthView, MeView

urlpatterns = [
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/me/", MeView.as_view(), name="me"),
    path("api/surgeon/", include("surgeonpilot.urls")),
    path("api/aidoccall/", include("aidoccall.urls")),
    path("api/payments/", include("payments.urls")),
]
