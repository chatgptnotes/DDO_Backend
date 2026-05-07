"""Pytest fixtures for the backend test suite."""
from __future__ import annotations

import os
import time

import jwt
import pytest


# Make sure tests use a deterministic settings module BEFORE Django is imported.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")


@pytest.fixture
def jwt_secret(settings):
    return settings.SUPABASE_JWT_SECRET


@pytest.fixture
def jwt_audience(settings):
    return settings.SUPABASE_JWT_AUDIENCE


@pytest.fixture
def make_token(jwt_secret, jwt_audience, settings):
    """Factory: produce a Supabase-shaped JWT for tests.

    Usage:
        token = make_token(sub="user-1", email="x@y.com")
        token = make_token(exp_offset=-60)  # expired
    """

    def _make(
        *,
        sub: str = "user-1",
        email: str = "test@example.com",
        exp_offset: int = 3600,
        audience: str | None = None,
        secret: str | None = None,
        algorithm: str | None = None,
        extra_claims: dict | None = None,
    ) -> str:
        now = int(time.time())
        payload = {
            "sub": sub,
            "email": email,
            "aud": audience if audience is not None else jwt_audience,
            "iat": now,
            "exp": now + exp_offset,
            "role": "authenticated",
        }
        if extra_claims:
            payload.update(extra_claims)
        return jwt.encode(
            payload,
            secret or jwt_secret,
            algorithm=algorithm or settings.SUPABASE_JWT_ALGORITHM,
        )

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
