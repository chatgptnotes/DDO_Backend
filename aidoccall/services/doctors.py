"""
Doctor-profile creation flow used by clinical_admin / superadmin.

Replaces the legacy frontend path that called Supabase Auth directly and
crashed with "email already exists" when the email belonged to an existing
user (the admin themselves, or any other identity in the system).

The flow is deliberately idempotent so a clinical_admin can resubmit the
form without producing duplicates:

    1. Look up auth.users by lower(email).
       - missing → call Supabase Admin API to create + invite.
    2. Insert into public.user_roles (user_id, 'doctor', scope_id) — no-op on
       conflict.
    3. Upsert into public.doc_doctors keyed by user_id — no-op when nothing
       changed; otherwise update fields.

`scope_id` (clinic) is supported but optional. The caller decides whether to
attach a scope.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from django.db import connection, transaction

from core.supabase_admin import find_or_create_user
from surgeonpilot.availability import create_default_availability

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DoctorCreationResult:
    user_id: str
    email: str
    was_new_user: bool
    role_granted: bool
    profile_created: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@transaction.atomic
def create_doctor_profile(
    *,
    caller_user_id: str,
    payload: dict[str, Any],
    scope_id: str | None = None,
) -> DoctorCreationResult:
    """Grant 'doctor' role to the email's owner and upsert their profile."""
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise ValueError("email is required")

    auth_user, was_new = find_or_create_user(email)
    user_id = auth_user.id

    role_granted = _grant_doctor_role(
        user_id=user_id,
        scope_id=scope_id,
        granted_by=caller_user_id,
    )
    profile_created = _upsert_doctor_profile(
        user_id=user_id,
        email=email,
        payload=payload,
    )
    if profile_created:
        # New doctors must be bookable immediately: patients can only pick
        # dates for doctors with active doc_availability rows.
        with connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM public.doc_doctors WHERE user_id = %s LIMIT 1",
                [user_id],
            )
            row = cur.fetchone()
        if row:
            create_default_availability(str(row[0]))

    logger.info(
        "create_doctor_profile email=%s user_id=%s new_user=%s "
        "role_granted=%s profile_created=%s",
        email,
        user_id,
        was_new,
        role_granted,
        profile_created,
    )

    return DoctorCreationResult(
        user_id=user_id,
        email=email,
        was_new_user=was_new,
        role_granted=role_granted,
        profile_created=profile_created,
    )


def _grant_doctor_role(
    *,
    user_id: str,
    scope_id: str | None,
    granted_by: str,
) -> bool:
    """Insert into user_roles. Returns True iff a new row was inserted."""
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.user_roles (user_id, role, scope_id, granted_by)
            VALUES (%s, 'doctor'::app_role, %s, %s)
            ON CONFLICT (user_id, role, scope_id) DO NOTHING
            RETURNING id
            """,
            [user_id, scope_id, granted_by],
        )
        return cur.fetchone() is not None


def _upsert_doctor_profile(
    *,
    user_id: str,
    email: str,
    payload: dict[str, Any],
) -> bool:
    """Insert or update a `doc_doctors` row keyed by user_id.

    Returns True iff a new row was created. Avoids ON CONFLICT (user_id) so
    we don't depend on a UNIQUE constraint that may not exist yet — instead
    do a SELECT-then-branch.
    """
    fields = {
        "full_name": payload.get("full_name"),
        "phone": payload.get("phone"),
        "specialization": payload.get("specialization"),
        "qualification": payload.get("qualification"),
        "experience_years": payload.get("experience_years"),
        "clinic_name": payload.get("clinic_name"),
        "clinic_address": payload.get("clinic_address"),
        "consultation_fee": payload.get("consultation_fee"),
        "online_fee": payload.get("online_fee"),
        "bio": payload.get("bio"),
    }
    fields = {k: v for k, v in fields.items() if v is not None}

    with connection.cursor() as cur:
        cur.execute(
            "SELECT id FROM public.doc_doctors WHERE user_id = %s LIMIT 1",
            [user_id],
        )
        existing = cur.fetchone()

        if existing is None:
            columns = ["user_id", "email", "full_name", *fields.keys()]
            values = [user_id, email, fields.get("full_name") or email]
            for k in fields:
                if k != "full_name":
                    values.append(fields[k])
            placeholders = ", ".join(["%s"] * len(values))
            cur.execute(
                f"""
                INSERT INTO public.doc_doctors ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                values,
            )
            return True

        if not fields:
            return False
        assignments = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(
            f"""
            UPDATE public.doc_doctors
               SET {assignments}, updated_at = NOW()
             WHERE user_id = %s
            """,
            [*fields.values(), user_id],
        )
        return False
