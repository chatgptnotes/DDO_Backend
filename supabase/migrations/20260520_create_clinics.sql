-- ============================================================================
-- 20260520_create_clinics.sql
--
-- Stripe Connect — clinic tenant model.
--
-- Today there is NO clinics table: `doc_doctors.clinic_id` is a loose UUID and
-- `clinic_name` is free text. Stripe Connect needs a real clinic entity to hold
-- the connected-account state, because patient payments route to the connected
-- account of the doctor's clinic (destination charges).
--
-- This migration is additive:
--   1. CREATE TABLE public.clinics            — clinic identity + Connect state
--   2. Backfill clinics from existing doc_doctors (group by created_by admin;
--      solo doctors get a 1-doctor clinic; pre-existing clinic_id values are
--      preserved as real clinic rows).
--   3. doc_doctors.clinic_id -> real FK to clinics(id)
--   4. doc_payment_intents   -> add clinic_id / stripe_connected_account_id /
--      transfer_id (Connect routing + refund clawback)
--   5. Backfill user_roles.scope_id for clinical_admin rows (authz anchor).
--
-- Apply order:
--   1. Run in STAGING first.
--   2. Run the "PROPOSED GROUPING" SELECT (below, commented) and have a human
--      review it BEFORE running the writes.
--   3. Verify 0 doctors with NULL clinic_id, then apply to prod off-peak.
--
-- No Stripe API calls happen here — every clinic starts `not_started` with a
-- NULL stripe_account_id. Accounts are created later via the onboarding flow.
-- ============================================================================

BEGIN;

-- ---- 1. clinics table ------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.clinics (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    contact_email       TEXT,
    country             CHAR(2),                       -- ISO-3166-1 alpha-2; immutable once the account is created
    created_by          UUID REFERENCES auth.users(id) ON DELETE SET NULL,

    -- Stripe Connect state — a denormalised mirror of the connected account.
    stripe_account_id   TEXT UNIQUE,                   -- acct_... ; NULL until onboarding starts
    stripe_livemode     BOOLEAN NOT NULL DEFAULT FALSE, -- test vs live acct_ ids are distinct objects
    onboarding_status   TEXT NOT NULL DEFAULT 'not_started'
                        CHECK (onboarding_status IN
                          ('not_started', 'pending', 'active', 'restricted', 'disabled')),
    charges_enabled     BOOLEAN NOT NULL DEFAULT FALSE, -- THE money-path guard reads this column
    payouts_enabled     BOOLEAN NOT NULL DEFAULT FALSE, -- a warning signal, not a charge blocker
    details_submitted   BOOLEAN NOT NULL DEFAULT FALSE,
    requirements_due    JSONB  NOT NULL DEFAULT '{}'::jsonb,
    default_currency    CHAR(3),
    account_updated_at  TIMESTAMPTZ,                   -- event.created of the last applied account.updated (monotonic guard)

    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.clinics IS
    'Clinic tenant. Holds one Stripe Connect (Express) connected account; patient payments route here.';
COMMENT ON COLUMN public.clinics.charges_enabled IS
    'Mirror of the connected account. The payment-routing guard checks this directly — never trust onboarding_status for the money path.';
COMMENT ON COLUMN public.clinics.account_updated_at IS
    'event.created of the last applied account.updated webhook. Older events are ignored (out-of-order guard).';

-- `set_updated_at_now()` already exists (created by 20260504_stripe_payments.sql).
DROP TRIGGER IF EXISTS trg_clinics_set_updated_at ON public.clinics;
CREATE TRIGGER trg_clinics_set_updated_at
    BEFORE UPDATE ON public.clinics
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at_now();

CREATE INDEX IF NOT EXISTS ix_clinics_created_by ON public.clinics (created_by);
-- UNIQUE on stripe_account_id already enforced by the column constraint; partial
-- index keeps NULLs cheap and the active-account lookup fast.
CREATE INDEX IF NOT EXISTS ix_clinics_stripe_account
    ON public.clinics (stripe_account_id) WHERE stripe_account_id IS NOT NULL;

-- ---- 2. Backfill clinics from existing doc_doctors -------------------------
-- Idempotent: every step is guarded so the migration can be re-run safely.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'doc_doctors'
    ) THEN
        RAISE NOTICE 'doc_doctors not present — skipping clinic backfill';
        RETURN;
    END IF;

    -- 2a. Preserve any pre-existing clinic_id groupings: materialise each
    --     distinct non-null clinic_id as a real clinics row with that exact id.
    --     This prevents the FK (step 3) from failing on legacy values.
    INSERT INTO public.clinics (id, name, contact_email, created_by)
    SELECT DISTINCT ON (d.clinic_id)
           d.clinic_id,
           COALESCE(NULLIF(TRIM(d.clinic_name), ''), 'Clinic'),
           d.email,
           d.created_by
    FROM public.doc_doctors d
    WHERE d.clinic_id IS NOT NULL
    ORDER BY d.clinic_id, d.created_at NULLS LAST
    ON CONFLICT (id) DO NOTHING;

    -- 2b. One clinic per clinical_admin who created doctors (the common case).
    INSERT INTO public.clinics (name, contact_email, created_by)
    SELECT
        COALESCE(
            (SELECT NULLIF(TRIM(d2.clinic_name), '')
               FROM public.doc_doctors d2
              WHERE d2.created_by = grp.created_by
                AND NULLIF(TRIM(d2.clinic_name), '') IS NOT NULL
              ORDER BY d2.created_at NULLS LAST
              LIMIT 1),
            'Clinic - ' || COALESCE(u.email, grp.created_by::text)
        ),
        u.email,
        grp.created_by
    FROM (
        SELECT DISTINCT created_by
        FROM public.doc_doctors
        WHERE created_by IS NOT NULL
          AND clinic_id IS NULL          -- only doctors not already grouped in 2a
    ) grp
    LEFT JOIN auth.users u ON u.id = grp.created_by
    WHERE NOT EXISTS (
        SELECT 1 FROM public.clinics c WHERE c.created_by = grp.created_by
    );

    -- 2c. Attach those doctors to their admin's clinic.
    UPDATE public.doc_doctors d
       SET clinic_id = c.id
      FROM public.clinics c
     WHERE d.created_by = c.created_by
       AND d.clinic_id IS NULL
       AND d.created_by IS NOT NULL;

    -- 2d. Solo doctors (no created_by) — a 1-doctor clinic each.
    DECLARE
        r          RECORD;
        new_clinic UUID;
    BEGIN
        FOR r IN
            SELECT id, full_name, email, clinic_name, user_id
            FROM public.doc_doctors
            WHERE clinic_id IS NULL
        LOOP
            INSERT INTO public.clinics (name, contact_email, created_by)
            VALUES (
                COALESCE(NULLIF(TRIM(r.clinic_name), ''), NULLIF(TRIM(r.full_name), ''), 'Clinic'),
                r.email,
                r.user_id
            )
            RETURNING id INTO new_clinic;

            UPDATE public.doc_doctors SET clinic_id = new_clinic WHERE id = r.id;
        END LOOP;
    END;
END$$;

-- ---- 3. doc_doctors.clinic_id -> real FK -----------------------------------
-- The column already exists (added by 20260319_add_tenant_isolation.sql).
-- After the backfill every clinic_id points to a real clinics row, so the FK
-- validates. clinic_id stays NULLABLE — a NULL is the natural "not yet onboarded
-- to a clinic" off-switch and the FK permits NULLs.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_schema = 'public'
          AND table_name = 'doc_doctors'
          AND constraint_name = 'fk_doc_doctors_clinic'
    ) THEN
        ALTER TABLE public.doc_doctors
            ADD CONSTRAINT fk_doc_doctors_clinic
            FOREIGN KEY (clinic_id) REFERENCES public.clinics(id) ON DELETE RESTRICT;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS ix_doc_doctors_clinic ON public.doc_doctors (clinic_id);

-- ---- 4. doc_payment_intents — Connect routing columns ----------------------
ALTER TABLE public.doc_payment_intents
    ADD COLUMN IF NOT EXISTS clinic_id                   UUID REFERENCES public.clinics(id),
    ADD COLUMN IF NOT EXISTS stripe_connected_account_id TEXT,   -- acct_... snapshot at create time (audit)
    ADD COLUMN IF NOT EXISTS transfer_id                 TEXT;   -- tr_... ; set by webhook, needed for reverse-transfer refunds

CREATE INDEX IF NOT EXISTS ix_payment_intents_clinic
    ON public.doc_payment_intents (clinic_id);

-- ---- 5. user_roles.scope_id backfill (clinical_admin -> their clinic) -------
-- scope_id is the authorization anchor: a clinical_admin may only manage the
-- clinic whose id equals their user_roles.scope_id.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'user_roles'
    ) THEN
        UPDATE public.user_roles ur
           SET scope_id = c.id
          FROM public.clinics c
         WHERE ur.role::text = 'clinical_admin'
           AND ur.scope_id IS NULL
           AND c.created_by = ur.user_id;
    END IF;
END$$;

-- ---- 6. RLS — defense in depth for direct PostgREST access -----------------
-- All Connect writes go through the Django backend (its DB role bypasses RLS).
-- These policies only constrain any direct frontend reads via Supabase.
ALTER TABLE public.clinics ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "scoped users see their clinic" ON public.clinics;
CREATE POLICY "scoped users see their clinic"
    ON public.clinics
    FOR SELECT
    USING (
        public.has_role('superadmin')
        OR EXISTS (
            SELECT 1 FROM public.user_roles ur
            WHERE ur.user_id = auth.uid()
              AND ur.is_active
              AND ur.scope_id = clinics.id
        )
        OR EXISTS (
            SELECT 1 FROM public.doc_doctors d
            WHERE d.clinic_id = clinics.id
              AND d.user_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS "superadmin manages clinics" ON public.clinics;
CREATE POLICY "superadmin manages clinics"
    ON public.clinics
    FOR ALL
    USING (public.has_role('superadmin'))
    WITH CHECK (public.has_role('superadmin'));

COMMIT;

-- ============================================================================
-- PROPOSED GROUPING — run this BEFORE the migration and have a human review it.
-- It shows how doctors will be grouped into clinics. Multi-admin clinics or
-- doctors shared across clinics need manual correction.
--
--   SELECT COALESCE(created_by::text, 'SOLO:' || id::text) AS clinic_key,
--          count(*) AS doctors,
--          string_agg(full_name, ', ' ORDER BY full_name) AS members
--   FROM public.doc_doctors
--   GROUP BY clinic_key
--   ORDER BY doctors DESC;
--
-- POST-APPLY VERIFICATION (must all pass before prod rollout):
--
--   -- Must be 0 — every doctor is attached to a clinic:
--   SELECT count(*) FROM public.doc_doctors WHERE clinic_id IS NULL;
--
--   -- Clinic count + doctors-per-clinic sanity check:
--   SELECT c.name, count(d.id) AS doctors
--   FROM public.clinics c
--   LEFT JOIN public.doc_doctors d ON d.clinic_id = c.id
--   GROUP BY c.id, c.name ORDER BY doctors DESC;
--
--   -- Every clinical_admin role is scoped to a clinic:
--   SELECT count(*) FROM public.user_roles
--   WHERE role::text = 'clinical_admin' AND is_active AND scope_id IS NULL;
-- ============================================================================
