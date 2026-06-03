"""
Role lookup utilities. Reads from the `user_roles` join table created by
`supabase/migrations/20260501_create_user_roles.sql`.

A user can hold multiple roles (e.g. a doctor who is also a clinical_admin
and also a patient). Active role is whatever the frontend sends in the
`X-Active-Role` header — we still verify the user actually holds it.
"""
from __future__ import annotations

from django.db import connection

ROLE_HEADER = "X-Active-Role"


def list_roles(user_id: str) -> list[str]:
    """Return all currently-active roles a user holds."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT role::text FROM user_roles "
            "WHERE user_id = %s AND is_active = true "
            "ORDER BY granted_at",
            [user_id],
        )
        return [row[0] for row in cur.fetchall()]


def has_role(user_id: str, *roles: str) -> bool:
    """True if the user holds any of the given roles, actively."""
    if not roles:
        return False
    with connection.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM user_roles "
            "WHERE user_id = %s AND is_active = true AND role::text = ANY(%s) "
            "LIMIT 1",
            [user_id, list(roles)],
        )
        return cur.fetchone() is not None


def clinic_scope_id(user_id: str) -> str | None:
    """Return the clinic id a user administers as `clinical_admin`.

    The `user_roles` row for a `clinical_admin` carries the clinic in
    `scope_id` (backfilled by `20260520_create_clinics.sql`). This is the
    authorization anchor for the Stripe Connect endpoints: a clinical admin
    may only manage the connected account of the clinic they are scoped to.

    Returns None if the user holds no active, clinic-scoped `clinical_admin`
    role. If they somehow hold more than one, the earliest-granted wins.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT scope_id::text FROM user_roles "
            "WHERE user_id = %s AND is_active = true "
            "  AND role::text = 'clinical_admin' AND scope_id IS NOT NULL "
            "ORDER BY granted_at "
            "LIMIT 1",
            [user_id],
        )
        row = cur.fetchone()
        return row[0] if row else None


def get_active_role(request, allowed: list[str]) -> str | None:
    """Read the requested active role from the header and validate it.

    Returns the role name if the user actually holds it, else None.
    The caller decides whether None means 403 or 'fall back to first role'.
    """
    requested = request.headers.get(ROLE_HEADER)
    if not requested or requested not in allowed:
        return None
    return requested
