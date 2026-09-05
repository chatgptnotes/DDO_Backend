"""Production settings — used in staging and prod deployments."""
from urllib.parse import unquote, urlparse

from decouple import config

from .base import *  # noqa: F401,F403

DEBUG = False

_db_url = config("DATABASE_URL")
_u = urlparse(_db_url)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote((_u.path or "/").lstrip("/")) or "postgres",
        "USER": unquote(_u.username or ""),
        # urlparse does not decode percent-escapes; passwords pasted into env
        # vars are commonly percent-encoded (e.g. @ as %40). dev.py already
        # unquotes for the same reason.
        "PASSWORD": unquote(_u.password or ""),
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

# The patient portal frontend (www.aidoccall.com) is hosted on a different
# site than this API, so the session/CSRF cookies must be explicitly
# cross-site. Requires Secure (set above) or browsers reject SameSite=None.
SESSION_COOKIE_SAMESITE = "None"
CSRF_COOKIE_SAMESITE = "None"
