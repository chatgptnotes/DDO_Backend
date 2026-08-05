"""
Base Django settings shared by dev and prod.
Environment-specific values (DEBUG, ALLOWED_HOSTS, DATABASES override) live in dev.py / prod.py.
"""
from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config("DJANGO_SECRET_KEY")

DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)

ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="", cast=Csv())

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "core",
    "surgeonpilot",
    "aidoccall",
    "payments",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        # Parsed from DATABASE_URL in dev.py / prod.py.
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---- DRF -------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "core.authentication.SupabaseJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "UNAUTHENTICATED_USER": None,
}

# ---- Consultation transcription -------------------------------------------
# Audio uploads for in-person consultation transcription. Default 25 MB covers
# a long consult of compressed opus; raise both together if needed. The
# DATA_UPLOAD limit must allow the multipart envelope to exceed the in-memory
# FILE_UPLOAD threshold (Django spools larger files to a temp file).
FILE_UPLOAD_MAX_MEMORY_SIZE = config(
    "FILE_UPLOAD_MAX_MEMORY_SIZE", default=25 * 1024 * 1024, cast=int
)
DATA_UPLOAD_MAX_MEMORY_SIZE = config(
    "DATA_UPLOAD_MAX_MEMORY_SIZE", default=26 * 1024 * 1024, cast=int
)

# Speech-to-text engine. 'google' = free SpeechRecognition Google Web Speech
# backend (requires ffmpeg on the host). 'whisper'/'gemini' reserved for later.
TRANSCRIPTION_ENGINE = config("TRANSCRIPTION_ENGINE", default="google")

# ---- Supabase JWT ----------------------------------------------------------
# Legacy symmetric secret (HS256). Still used for HS256-signed tokens and the
# test suite. Newer Supabase projects sign access tokens asymmetrically
# (ES256/RS256) with rotating keys — those are verified against the project's
# public JWKS instead (see SUPABASE_JWKS_URL below).
SUPABASE_JWT_SECRET = config("SUPABASE_JWT_SECRET")
SUPABASE_JWT_AUDIENCE = config("SUPABASE_JWT_AUDIENCE", default="authenticated")
SUPABASE_JWT_ALGORITHM = config("SUPABASE_JWT_ALGORITHM", default="HS256")

# Public JWKS endpoint for asymmetric (ES256/RS256) access tokens. Defaults to
# the project's well-known URL derived from SUPABASE_URL; override only if needed.
_SUPABASE_URL_FOR_JWKS = config("SUPABASE_URL", default="").rstrip("/")
SUPABASE_JWKS_URL = config(
    "SUPABASE_JWKS_URL",
    default=(
        f"{_SUPABASE_URL_FOR_JWKS}/auth/v1/.well-known/jwks.json"
        if _SUPABASE_URL_FOR_JWKS
        else ""
    ),
)

# Supabase project URL + service-role key — used for privileged operations
# (auth.admin.createUser, sending invite emails). Service role bypasses RLS,
# so it must NEVER reach the browser. Empty defaults so dev environments
# without these set still boot; views that need them raise a clear error.
SUPABASE_URL = config("SUPABASE_URL", default="")
SUPABASE_ANON_KEY = config("SUPABASE_ANON_KEY", default="")
SUPABASE_SERVICE_ROLE_KEY = config("SUPABASE_SERVICE_ROLE_KEY", default="")

# ---- CORS ------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())
CORS_ALLOW_CREDENTIALS = True

# ---- Stripe ----------------------------------------------------------------
# Server-side secret — never expose to the frontend. Must be set in production
# before /api/payments/ endpoints are usable. Tests can leave these empty.
STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY", default="")
STRIPE_PUBLISHABLE_KEY = config("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET", default="")

# ---- Stripe Connect --------------------------------------------------------
# Separate webhook signing secret for the Connect endpoint
# (/api/payments/webhooks/stripe/connect/). Stripe delivers connected-account
# events (account.updated) on a distinct endpoint with its own secret — the
# handler fails closed if this is missing, same as the platform webhook.
STRIPE_CONNECT_WEBHOOK_SECRET = config("STRIPE_CONNECT_WEBHOOK_SECRET", default="")

# Master switch for routing patient payments through Connect destination
# charges. When False, payment creation uses the legacy single-account path.
# Flip to True only once clinics have onboarded (Phase 2 of the rollout plan).
CONNECT_ENABLED = config("CONNECT_ENABLED", default=False, cast=bool)

# Where Stripe-hosted Express onboarding returns the clinical admin. Both URLs
# point at the AiSurgeonPilot clinic-admin Payments page: `refresh` re-mints an
# expired Account Link, `return` lands on the "verifying…" state that polls
# /api/payments/connect/status/.
CONNECT_ONBOARDING_RETURN_URL = config(
    "CONNECT_ONBOARDING_RETURN_URL",
    default="http://localhost:3000/clinic-admin/payments?stripe=return",
)
CONNECT_ONBOARDING_REFRESH_URL = config(
    "CONNECT_ONBOARDING_REFRESH_URL",
    default="http://localhost:3000/clinic-admin/payments?stripe=refresh",
)

# ---- Email -----------------------------------------------------------------
# Defaults to Django's console backend in dev — flip via env in prod.
EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = config("EMAIL_HOST", default="")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
DEFAULT_FROM_EMAIL = config(
    "DEFAULT_FROM_EMAIL",
    default="AiDocCall <no-reply@aidoccall.com>",
)

# ---- Logging ---------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "core": {"level": "INFO"},
    },
}
