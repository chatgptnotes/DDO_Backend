"""Create the local clinic tenant table used by new Clinic Admin accounts.

This intentionally does not backfill or change ``doc_doctors``.  Existing
Supabase-era profiles remain untouched; the Clinic Admin creation endpoint
attaches only newly-created profiles to a newly-created clinic.
"""

from django.db import migrations


CREATE_CLINICS_SQL = """
CREATE TABLE IF NOT EXISTS public.clinics (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    contact_email TEXT,
    country CHAR(2),
    created_by UUID REFERENCES public.users(id) ON DELETE SET NULL,
    stripe_account_id TEXT UNIQUE,
    stripe_livemode BOOLEAN NOT NULL DEFAULT FALSE,
    onboarding_status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (onboarding_status IN ('not_started', 'pending', 'active', 'restricted', 'disabled')),
    charges_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    payouts_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    details_submitted BOOLEAN NOT NULL DEFAULT FALSE,
    requirements_due JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_currency CHAR(3),
    account_updated_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_clinics_created_by ON public.clinics (created_by);
CREATE INDEX IF NOT EXISTS ix_clinics_stripe_account
    ON public.clinics (stripe_account_id) WHERE stripe_account_id IS NOT NULL;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0007_ensure_public_users_table")]

    operations = [migrations.RunSQL(CREATE_CLINICS_SQL, reverse_sql=migrations.RunSQL.noop)]
