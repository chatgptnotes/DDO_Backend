"""Test settings — never hits a real database or a real signing secret."""
import os

# Hardcoded, deterministic test secret. Never used outside the test suite.
os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-not-for-prod")
os.environ.setdefault("AUTH_SESSION_SECRET", "test-session-secret-not-for-prod")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "*")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "")

from .base import *  # noqa: F401,F403,E402

DEBUG = False

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Disable DRF default pagination / throttling in tests for predictability.
REST_FRAMEWORK = {  # noqa: F405
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "core.authentication.LocalTokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "UNAUTHENTICATED_USER": None,
}
