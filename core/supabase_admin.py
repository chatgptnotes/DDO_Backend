"""
Server-side helper for the Supabase Auth Admin REST API.

Why this exists:
    `auth.admin.createUser` and friends require the service-role key, which
    bypasses RLS. The service-role key MUST NEVER reach the browser, so all
    such calls are funnelled through Django.

Stdlib `urllib` is used deliberately — `requests`/`httpx` aren't in
`requirements.txt` and pulling them in just for two endpoints isn't worth the
churn. If the surface grows we can swap to `httpx`.
"""
from __future__ import annotations

import json
import logging
import secrets
import string
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from django.conf import settings

logger = logging.getLogger(__name__)


class SupabaseAdminError(RuntimeError):
    """Raised when the Supabase Admin API returns a non-2xx response."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Supabase Admin API {status}: {body}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class AuthUser:
    """A subset of the Supabase auth.users record we care about."""

    id: str
    email: str


def _config() -> tuple[str, str]:
    base = (settings.SUPABASE_URL or "").rstrip("/")
    key = settings.SUPABASE_SERVICE_ROLE_KEY or ""
    if not base or not key:
        raise SupabaseAdminError(
            status=0,
            body="SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not configured",
        )
    return base, key


def _request(method: str, path: str, body: dict | None = None) -> dict:
    base, key = _config()
    url = f"{base}/auth/v1{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url=url, method=method, data=data)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode("utf-8") or "{}"
            return json.loads(payload)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise SupabaseAdminError(status=exc.code, body=body) from exc
    except error.URLError as exc:
        raise SupabaseAdminError(status=0, body=str(exc.reason)) from exc


def find_user_by_email(email: str) -> AuthUser | None:
    """Return the auth.users record matching the given email, or None.

    GoTrue's `GET /admin/users?email=` is unreliable across versions — some
    deployments ignore the filter and return the first page of users
    regardless. We always verify the returned email matches client-side, and
    page through results until we find a hit or exhaust the list. This makes
    the function safe to use as the gatekeeper for "does this email already
    have an auth user?"
    """
    normalized = email.strip().lower()
    if not normalized:
        return None

    # Try the filtered endpoint first; if it actually filters, we'll match on
    # the first page. If it returns everyone, we'll page through.
    page = 1
    per_page = 200
    while True:
        path = (
            f"/admin/users?email={request.quote(normalized)}"
            f"&page={page}&per_page={per_page}"
        )
        data = _request("GET", path)

        if isinstance(data, dict):
            users = data.get("users") or data.get("data") or []
        elif isinstance(data, list):
            users = data
        else:
            users = []

        if not users:
            return None

        for user in users:
            user_email = str(user.get("email") or "").strip().lower()
            if user_email == normalized:
                return AuthUser(id=str(user["id"]), email=user_email)

        # Stop paging once we've seen fewer than a full page.
        if len(users) < per_page:
            return None
        page += 1
        if page > 50:  # hard ceiling — 10k users is enough for any reasonable tenant
            logger.warning(
                "find_user_by_email exhausted page cap looking for %s", normalized
            )
            return None


def create_user(email: str, *, send_invite: bool = True) -> AuthUser:
    """Create a new Supabase auth user and (by default) send an invite email.

    With `send_invite=True`, GoTrue's `/admin/invite` endpoint is used so the
    user receives a magic link to set their password. Otherwise we provision
    a random password the caller must rotate.
    """
    normalized = email.strip().lower()
    if not normalized:
        raise SupabaseAdminError(status=0, body="email is required")

    if send_invite:
        data = _request("POST", "/admin/invite", body={"email": normalized})
    else:
        password = _random_password()
        data = _request(
            "POST",
            "/admin/users",
            body={
                "email": normalized,
                "password": password,
                "email_confirm": True,
            },
        )

    user = data.get("user") if isinstance(data, dict) and "user" in data else data
    if not user or "id" not in user:
        logger.error("Supabase admin returned unexpected payload: %s", data)
        raise SupabaseAdminError(status=0, body=f"unexpected response: {data!r}")
    return AuthUser(id=str(user["id"]), email=str(user.get("email") or normalized))


def find_or_create_user(email: str) -> tuple[AuthUser, bool]:
    """Idempotent: return (user, was_new). Sends invite email when new."""
    existing = find_user_by_email(email)
    if existing is not None:
        return existing, False
    created = create_user(email, send_invite=True)
    return created, True


def _random_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# Public API
__all__ = [
    "AuthUser",
    "SupabaseAdminError",
    "create_user",
    "find_or_create_user",
    "find_user_by_email",
]
