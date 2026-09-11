"""
Local token authentication for Django REST Framework.

Clients sign in against the LOCAL PostgreSQL credential store (`auth.users`
bcrypt) and receive a signed session token —
`base64url(payload).base64url(hmac)` with an HMAC-SHA256 signature over the
payload using the shared ``AUTH_SESSION_SECRET``. The Next.js portal issues
the same token shape for its `ddo_session` cookie; both apps verify it with
the shared secret and resolve the caller by email against local PostgreSQL.

This backend never sees passwords. Token issuing stays with the login
endpoints; verification here is stateless.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

logger = logging.getLogger("core")


@dataclass
class LocalPrincipal:
    """A lightweight, immutable principal derived from a verified token.

    DRF treats `is_authenticated = True` as the signal that auth succeeded.
    No password is ever held here — credentials stay in local PostgreSQL.
    """

    id: str
    email: str | None
    payload: dict[str, Any]
    is_authenticated: bool = True

    @property
    def pk(self) -> str:
        return self.id

    def __str__(self) -> str:
        return self.email or self.id


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _verify_session_token(token: str, secret: str) -> dict[str, Any] | None:
    """Verify the signed session token; return its payload, or None."""
    try:
        data, signature = token.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(secret.encode("utf-8"), data.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64url_decode(signature)
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(expected, provided):
        return None

    try:
        payload = json.loads(_b64url_decode(data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("email"):
        return None

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None

    return payload


class LocalTokenAuthentication(BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request: Request):
        header = request.headers.get("Authorization", "")
        if not header:
            return None
        parts = header.split(" ", 1)
        if len(parts) != 2 or parts[0] != self.keyword or not parts[1].strip():
            return None
        token = parts[1].strip()

        secret = getattr(settings, "AUTH_SESSION_SECRET", "")
        if not secret:
            logger.error("AUTH_SESSION_SECRET is not configured — rejecting token")
            raise AuthenticationFailed("Token verification unavailable")

        payload = _verify_session_token(token, secret)
        if payload is None:
            raise AuthenticationFailed("Invalid or expired token")

        user = LocalPrincipal(
            id=str(payload.get("sub") or payload["email"]),
            email=payload.get("email"),
            payload=payload,
        )
        return (user, token)

    def authenticate_header(self, request: Request) -> str:
        return f'{self.keyword} realm="api"'
