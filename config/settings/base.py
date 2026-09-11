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

# ---- Custom User Model -------------------------------------------------------
AUTH_USER_MODEL = "core.LocalUser"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Local session login supports Django-hashed passwords in public.users and,
# temporarily, legacy bcrypt hashes kept in the local auth.users table.  The
# compatibility backend makes no network call to Supabase.
AUTHENTICATION_BACKENDS = [
    "core.backends.GotrueAuthBackend",
    "django.contrib.auth.backends.ModelBackend",
]

# ---- DRF -------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # New browser sessions authenticate directly against PostgreSQL.
        "rest_framework.authentication.SessionAuthentication",
        # Keep existing bearer-token clients working while their individual
        # features are migrated away from Supabase.
        "core.authentication.LocalTokenAuthentication",
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

# ---- Local session tokens --------------------------------------------------
# Shared HMAC secret for the signed session tokens issued by the login
# endpoints (Next.js `ddo_session` cookie / Bearer tokens and this backend's
# own session login). Verified by core.authentication.LocalTokenAuthentication.
AUTH_SESSION_SECRET = config("AUTH_SESSION_SECRET", default="")

# ---- CORS ------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())
CORS_ALLOW_CREDENTIALS = True
# Origins allowed to submit CSRF-protected requests with a Django session.
# Production should use a same-site API host (for example api.aidoccall.com)
# so session cookies remain first-party.
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

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
