-- ============================================================================
-- 20260501_create_user_roles.sql
--
-- Multi-role identity for one Supabase user.
-- Solves: today, doc_doctors.role and public.users.role each store ONE role,
-- so an email cannot simultaneously be a doctor and a patient. After this
-- migration, a user can hold any combination of roles, optionally scoped to
-- a clinic / org.
--
-- Apply order:
--   1. Run this in the staging Supabase project first.
--   2. Verify backfill matches expectations (counts per role).
--   3. Apply to prod during a low-traffic window.
--
-- This migration is purely additive. It does NOT drop the existing
-- doc_doctors.role or public.users.role columns. Those become "primary
-- display role" and can be removed in a follow-up migration after all
-- frontend code reads from user_roles.
-- ============================================================================

BEGIN;

-- ---- 1. Enum of canonical roles --------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'app_role') THEN
        CREATE TYPE app_role AS ENUM (
            'superadmin',
            'clinical_admin',
            'doctor',
            'patient',
            'telecaller',
            'driver',
            'hospital'
        );
    END IF;
END$$;

-- ---- 2. Join table ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    role        app_role NOT NULL,
    scope_id    UUID,                           -- clinic_id / org_id; NULL = globalwhy 
    is_active   BOOLEAN NOT NULL DEFAULT true,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by  UUID REFERENCES auth.users(id),
    revoked_at  TIMESTAMPTZ,
    revoked_by  UUID REFERENCES auth.users(id),
    UNIQUE (user_id, role, scope_id)
);

CREATE INDEX IF NOT EXISTS user_roles_user_active_idx
    ON public.user_roles (user_id) WHERE is_active = true;

CREATE INDEX IF NOT EXISTS user_roles_role_idx
    ON public.user_roles (role) WHERE is_active = true;

-- ---- 3. Helper for RLS -----------------------------------------------------
CREATE OR REPLACE FUNCTION public.has_role(target_role app_role)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.user_roles
        WHERE user_id = auth.uid()
          AND role    = target_role
          AND is_active
    );
$$;

-- Variadic version: has_any_role('doctor', 'clinical_admin')
CREATE OR REPLACE FUNCTION public.has_any_role(VARIADIC target_roles app_role[])
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.user_roles
        WHERE user_id = auth.uid()
          AND role    = ANY(target_roles)
          AND is_active
    );
$$;

-- ---- 4. RLS on user_roles itself -------------------------------------------
ALTER TABLE public.user_roles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "users see own roles" ON public.user_roles;
CREATE POLICY "users see own roles"
    ON public.user_roles
    FOR SELECT
    USING (user_id = auth.uid());

DROP POLICY IF EXISTS "superadmin manages all roles" ON public.user_roles;
CREATE POLICY "superadmin manages all roles"
    ON public.user_roles
    FOR ALL
    USING (public.has_role('superadmin'))
    WITH CHECK (public.has_role('superadmin'));

-- ---- 5. Backfill from existing single-role columns -------------------------
-- AiSurgeonPilot's doc_doctors table -> user_roles
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'doc_doctors'
    ) THEN
        INSERT INTO public.user_roles (user_id, role, granted_at)
        SELECT
            d.user_id,
            CASE d.role
                WHEN 'superadmin'     THEN 'superadmin'::app_role
                WHEN 'admin_clinical' THEN 'clinical_admin'::app_role
                WHEN 'doctor'         THEN 'doctor'::app_role
                ELSE 'doctor'::app_role
            END,
            COALESCE(d.created_at, now())
        FROM public.doc_doctors d
        WHERE d.user_id IS NOT NULL
          AND d.role IS NOT NULL
        ON CONFLICT (user_id, role, scope_id) DO NOTHING;
    END IF;
END$$;

-- aidoccall.com's public.users table -> user_roles
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'users'
    ) THEN
        INSERT INTO public.user_roles (user_id, role, granted_at)
        SELECT
            u.auth_user_id,
            CASE u.role
                WHEN 'admin'      THEN 'clinical_admin'::app_role
                WHEN 'driver'     THEN 'driver'::app_role
                WHEN 'hospital'   THEN 'hospital'::app_role
                WHEN 'patient'    THEN 'patient'::app_role
                WHEN 'telecaller' THEN 'telecaller'::app_role
                ELSE 'patient'::app_role
            END,
            COALESCE(u.created_at, now())
        FROM public.users u
        WHERE u.auth_user_id IS NOT NULL
          AND u.role IS NOT NULL
        ON CONFLICT (user_id, role, scope_id) DO NOTHING;
    END IF;
END$$;

COMMIT;

-- ============================================================================
-- Post-apply verification (run manually):
--
--   SELECT role, count(*) FROM public.user_roles GROUP BY role ORDER BY 1;
--   SELECT auth.uid();  -- should match a real user_id during a logged-in session
--   SELECT public.has_role('doctor');
-- ============================================================================
