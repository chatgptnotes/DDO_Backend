"""Production settings — used in staging and prod deployments."""
from urllib.parse import urlparse

from decouple import config

from .base import *  # noqa: F401,F403

DEBUG = False

_db_url = config("DATABASE_URL")
_u = urlparse(_db_url)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": (_u.path or "/").lstrip("/") or "postgres",
        "USER": _u.username or "",
        "PASSWORD": _u.password or "",
        "HOST": _u.hostname or "",
        "PORT": str(_u.port or 5432),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"sslmode": "require"},
    }
}

# Security headers (extend CSP via reverse proxy as needed)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "DENY"
