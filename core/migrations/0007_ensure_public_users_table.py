"""Ensure the PostgreSQL application user table exists in the public schema.

Older local databases recorded migration 0003 while resolving its unqualified
``users`` identifier to ``auth.users``.  This repair is schema-qualified and
non-destructive: it creates public.users only when missing and copies legacy
auth identities without changing auth data.
"""

from django.db import migrations


FORWARD_SQL = """
CREATE TABLE IF NOT EXISTS public.users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_user_id uuid,
    email text UNIQUE,
    full_name text,
    role text DEFAULT 'patient',
    phone text,
    password text,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    employee_id varchar(255),
    shift_timing varchar(255),
    specialization_focus varchar(255),
    supervisor_id uuid,
    hire_date date DEFAULT CURRENT_DATE,
    department varchar(255) DEFAULT 'Customer Support',
    salary_range varchar(255),
    performance_rating numeric DEFAULT 0.00,
    last_login timestamptz,
    is_indian_resident boolean
);

DO $$
BEGIN
    IF to_regclass('auth.users') IS NOT NULL THEN
        INSERT INTO public.users (
            id, auth_user_id, email, full_name, role, phone,
            created_at, updated_at
        )
        SELECT
            a.id,
            a.id,
            a.email,
            COALESCE(a.raw_user_meta_data->>'full_name', ''),
            COALESCE(a.raw_user_meta_data->>'role', 'patient'),
            a.phone,
            COALESCE(a.created_at, now()),
            COALESCE(a.updated_at, now())
        FROM auth.users a
        WHERE a.email IS NOT NULL
        ON CONFLICT DO NOTHING;
    END IF;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0006_user_roles_link_to_public_users")]

    operations = [
        migrations.RunSQL(FORWARD_SQL, reverse_sql=migrations.RunSQL.noop),
    ]
