"""
Supabase JWT authentication for Django REST Framework.

Frontends sign in via Supabase Auth and receive a JWT. They forward that JWT
to this backend in the `Authorization: Bearer <token>` header. We verify the
signature with the Supabase project's JWT secret (HS256 by default) and
expose the decoded payload as `request.user`.

This backend never sees passwords. Token issuing, refresh, MFA, and password
reset all stay with Supabase.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import jwt
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

logger = logging.getLogger("core")

# Asymmetric algorithms whose tokens are verified against the project's public
# JWKS rather than the legacy shared secret.
ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")

# Lazily-built, cached JWKS client (caches fetched signing keys internally).
_jwks_client: "jwt.PyJWKClient | None" = None


def _get_jwks_client() -> "jwt.PyJWKClient":
    global _jwks_client
    if _jwks_client is None:
        url = getattr(settings, "SUPABASE_JWKS_URL", "")
        if not url:
            raise AuthenticationFailed("Asymmetric token verification is not configured")
        _jwks_client = jwt.PyJWKClient(url)
    return _jwks_client


@dataclass
class SupabaseUser:
    """A lightweight, immutable principal derived from a verified JWT.

    DRF treats `is_authenticated = True` as the signal that auth succeeded.
    No password is ever held here — Supabase owns credentials.
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


class SupabaseJWTAuthentication(BaseAuthentication):
    keyword = "Bearer"

    @staticmethod
    def _validate_with_supabase(token: str) -> dict[str, Any] | None:
        """Validate a session against Supabase when the local signing key is stale."""
        if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
            return None

        request = urllib.request.Request(
            f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/user",
            method="GET",
            headers={
                "apikey": settings.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None

        return payload if isinstance(payload, dict) and payload.get("id") else None

    def authenticate(self, request: Request):
        header = request.headers.get("Authorization", "")
        if not header:
            return None
        parts = header.split(" ", 1)
        if len(parts) != 2 or parts[0] != self.keyword or not parts[1].strip():
            return None
        token = parts[1].strip()

        # Route by the token's signing algorithm: newer Supabase projects sign
        # access tokens asymmetrically (ES256) with rotating keys published via
        # JWKS; older projects (and our test suite) use the HS256 shared secret.
        try:
            alg = jwt.get_unverified_header(token).get("alg", "")
        except jwt.InvalidTokenError as exc:
            raise AuthenticationFailed("Invalid token") from exc

        try:
            if alg in ASYMMETRIC_ALGORITHMS:
                signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
                payload = jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=list(ASYMMETRIC_ALGORITHMS),
                    audience=settings.SUPABASE_JWT_AUDIENCE,
                    options={"require": ["exp", "sub"]},
                )
            else:
                payload = jwt.decode(
                    token,
                    settings.SUPABASE_JWT_SECRET,
                    algorithms=[settings.SUPABASE_JWT_ALGORITHM],
                    audience=settings.SUPABASE_JWT_AUDIENCE,
                    options={"require": ["exp", "sub"]},
                )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationFailed("Token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationFailed("Invalid token audience") from exc
        except jwt.PyJWKClientError as exc:
            logger.error("JWKS key resolution failed: %s", exc)
            raise AuthenticationFailed("Invalid token") from exc
        except jwt.InvalidTokenError as exc:
            supabase_user = self._validate_with_supabase(token)
            if not supabase_user:
                raise AuthenticationFailed("Invalid token") from exc
            payload = {
                "sub": str(supabase_user["id"]),
                "email": supabase_user.get("email"),
            }

        user_id = payload.get("sub")
        if not user_id:
            raise AuthenticationFailed("Token missing subject")

        user = SupabaseUser(
            id=str(user_id),
            email=payload.get("email"),
            payload=payload,
        )
        return (user, token)

    def authenticate_header(self, request: Request) -> str:
        return f'{self.keyword} realm="api"'
