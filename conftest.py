"""Pytest fixtures for the backend test suite."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import pytest


# Make sure tests use a deterministic settings module BEFORE Django is imported.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.fixture
def session_secret(settings):
    return settings.AUTH_SESSION_SECRET


@pytest.fixture
def make_token(session_secret):
    """Factory: produce a signed LOCAL session token for tests.

    Usage:
        token = make_token(sub="user-1", email="x@y.com")
        token = make_token(exp_offset=-60)  # expired
    """

    def _make(
        *,
        sub: str = "user-1",
        email: str = "test@example.com",
        exp_offset: int = 3600,
        role: str = "authenticated",
        secret: str | None = None,
        extra_claims: dict | None = None,
    ) -> str:
        now = int(time.time())
        payload = {
            "email": email,
            "role": role,
            "exp": now + exp_offset,
        }
        if sub:
            payload["sub"] = sub
        if extra_claims:
            payload.update(extra_claims)
        data = _b64url_encode(json.dumps(payload).encode("utf-8"))
        signature = hmac.new(
            (secret or session_secret).encode("utf-8"),
            data.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{data}.{_b64url_encode(signature)}"

    return _make


@pytest.fixture
def auth_header(make_token):
    """Convenience: returns dict suitable for `client.get(..., **auth_header)`."""

    def _header(**kwargs) -> dict[str, str]:
        token = make_token(**kwargs)
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    return _header


@pytest.fixture
def patch_roles(monkeypatch):
    """Stub out the user_roles DB lookups so tests don't need a Postgres DB.

    Usage:
        patch_roles(roles_for_user="user-1", roles=["doctor", "patient"])
    """

    state: dict[str, list[str]] = {}

    def _set(user_id: str, roles: list[str]) -> None:
        state[user_id] = list(roles)

    monkeypatch.setattr(
        "core.roles.list_roles",
        lambda user_id: list(state.get(user_id, [])),
    )
    monkeypatch.setattr(
        "core.roles.has_role",
        lambda user_id, *roles: any(r in state.get(user_id, []) for r in roles),
    )
    monkeypatch.setattr(
        "core.permissions.has_role",
        lambda user_id, *roles: any(r in state.get(user_id, []) for r in roles),
    )

    return _set
