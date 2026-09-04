"""
Local GoTrue-equivalent signups: insert directly into `auth.users`.

The local Postgres is a full Supabase schema copy (auth schema included) but
runs no GoTrue server. Patient registration therefore writes the same rows
Supabase Auth would: an `auth.users` row with a GoTrue-readable bcrypt
`encrypted_password`, plus the matching `auth.identities` email identity.

Row shape mirrors existing rows in this database:
  instance_id            zero uuid (Supabase's default project instance)
  aud / role             'authenticated'
  email_confirmed_at     now() — local accounts are auto-confirmed
  raw_app_meta_data      {"provider": "email", "providers": ["email"]}
  raw_user_meta_data     {"sub", "email", "role", ...} like supabase-js signUps

`public.users` remains the app-level mirror (Django's LocalUser) keyed by the
SAME uuid, matching the legacy auth.users -> public.users relationship.
"""
from __future__ import annotations

import json
import uuid

import bcrypt
from django.db import connection

ZERO_INSTANCE_ID = "00000000-0000-0000-0000-000000000000"


def bcrypt_password(plain: str) -> str:
    """Hash like GoTrue: $2a$10$ bcrypt, readable by Supabase Auth."""
    return bcrypt.hashpw(
        plain.encode("utf-8"),
        bcrypt.gensalt(rounds=10, prefix=b"2a"),
    ).decode("ascii")


def auth_user_exists(email: str) -> bool:
    """True if a non-SSO auth user already holds this email (case-insensitive)."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM auth.users "
            "WHERE lower(email) = lower(%s) AND is_sso_user = false LIMIT 1",
            [email],
        )
        return cur.fetchone() is not None


def verify_gotrue_credentials(email: str, password: str) -> bool:
    """Check email + password against auth.users' bcrypt `encrypted_password`."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT encrypted_password FROM auth.users "
            "WHERE lower(email) = lower(%s) AND is_sso_user = false LIMIT 1",
            [email],
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), row[0].encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False


def create_gotrue_user(
    user_id: uuid.UUID,
    email: str,
    password: str,
    *,
    full_name: str = "",
    phone: str = "",
    is_indian_resident: bool | None = None,
    role: str = "patient",
) -> None:
    """Insert the auth.users + auth.identities rows for a new local signup.

    The `on_auth_user_created` DB trigger then mirrors the row into
    `public.users` (full_name/role/phone from the metadata below, linked via
    `auth_user_id`) — no second ORM insert needed here. Caller wraps this in
    transaction.atomic with any profile insert.
    """
    user_meta = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "full_name": full_name,
        "phone": phone,
    }
    if is_indian_resident is not None:
        user_meta["is_indian_resident"] = is_indian_resident

    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO auth.users (
                instance_id, id, aud, role, email, encrypted_password,
                email_confirmed_at, last_sign_in_at,
                raw_app_meta_data, raw_user_meta_data,
                created_at, updated_at, is_sso_user, is_anonymous
            ) VALUES (
                %s::uuid, %s::uuid, 'authenticated', 'authenticated', %s, %s,
                now(), now(),
                %s::jsonb, %s::jsonb,
                now(), now(), false, false
            )
            """,
            [
                ZERO_INSTANCE_ID,
                str(user_id),
                email,
                bcrypt_password(password),
                json.dumps({"provider": "email", "providers": ["email"]}),
                json.dumps(user_meta),
            ],
        )
        cur.execute(
            """
            INSERT INTO auth.identities (
                id, user_id, provider_id, provider, identity_data,
                last_sign_in_at, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), %s::uuid, %s, 'email', %s::jsonb,
                now(), now(), now()
            )
            ON CONFLICT (provider_id, provider) DO NOTHING
            """,
            [
                str(user_id),
                email,
                json.dumps({"sub": str(user_id), "email": email, "email_verified": True}),
            ],
        )
