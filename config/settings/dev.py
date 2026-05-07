"""Local development settings.

DATABASE_URL must be supplied via env (.env locally, real env vars on a server).
There is no sqlite fallback — we always run against real Postgres so dev
behaviour matches staging and prod.

The URL parser tolerates UNENCODED special characters in the password
(common when copy-pasting straight from the Supabase dashboard). It splits
the URL on the LAST '@' before the host, so passwords containing '@' or
other URL-special characters work without manual percent-encoding.
"""
from decouple import config

from .base import *  # noqa: F401,F403


def _parse_postgres_url(url: str) -> dict:
    """Robustly split a postgres://... URL even when the password is unencoded.

    Returns {ENGINE, NAME, USER, PASSWORD, HOST, PORT}. Falls back to whatever
    fields can be parsed; missing pieces are returned as empty strings.
    """
    if "://" not in url:
        raise ValueError("DATABASE_URL must include a scheme like postgresql://")
    _scheme, rest = url.split("://", 1)

    # rsplit on '@' once: everything left of the LAST '@' is auth, right is host[:port][/db][?params]
    if "@" not in rest:
        raise ValueError("DATABASE_URL is missing '@<host>' separator")
    auth_part, host_part = rest.rsplit("@", 1)

    user, password = (auth_part.split(":", 1) + [""])[:2] if ":" in auth_part else (auth_part, "")

    # host[:port]/dbname[?params]
    host_section, _, path_section = host_part.partition("/")
    db_section, _, _params = path_section.partition("?")
    host, _, port = host_section.partition(":")

    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": db_section or "postgres",
        "USER": user or "",
        "PASSWORD": password or "",
        "HOST": host or "",
        "PORT": port or "5432",
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"sslmode": "require"},
    }


DEBUG = True
ALLOWED_HOSTS = ["*"]

DATABASES = {"default": _parse_postgres_url(config("DATABASE_URL"))}
