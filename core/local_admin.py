"""
Local auth-user provisioning (replaces the Supabase Admin API client).

Doctor onboarding needs an `auth.users` row to exist before a profile can be
linked. Provisioning is now entirely local:

    1. Look up auth.users by lower(email).
    2. Missing → create the GoTrue-shaped row locally (bcrypt, random
       password) via core.gotrue_local. No invite email is sent — Django's
       own password-reset flow assigns credentials.

The flow is idempotent so a clinical_admin can resubmit the form without
producing duplicates.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass

from django.db import connection

from core import gotrue_local

logger = logging.getLogger("core")


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str


class LocalProvisioningError(Exception):
    """Raised when the local auth row could not be provisioned."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _find_user_by_email(email: str) -> AuthUser | None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, lower(email) FROM auth.users "
            "WHERE lower(email) = %s AND is_sso_user = false LIMIT 1",
            [email],
        )
        row = cur.fetchone()
    if not row:
        return None
    return AuthUser(id=str(row[0]), email=str(row[1]))


def _create_local_user(email: str) -> AuthUser:
    user_id = uuid.uuid4()
    password = _random_password()
    try:
        gotrue_local.create_gotrue_user(
            user_id=user_id,
            email=email,
            password=password,
            full_name="",
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a provisioning failure
        logger.error("Local auth user provisioning failed: %s", exc)
        raise LocalProvisioningError(f"could not provision auth user: {exc}") from exc
    return AuthUser(id=str(user_id), email=email.lower())


def _random_password(length: int = 32) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def find_or_create_local_user(email: str) -> tuple[AuthUser, bool]:
    """Idempotent: return (user, was_new)."""
    normalized = (email or "").strip().lower()
    existing = _find_user_by_email(normalized)
    if existing is not None:
        return existing, False
    created = _create_local_user(normalized)
    return created, True


# Public API
__all__ = [
    "AuthUser",
    "LocalProvisioningError",
    "find_or_create_local_user",
]
