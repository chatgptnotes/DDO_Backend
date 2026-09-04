"""Move the existing doc_patients account link from auth.users to public.users.

No patient profile is deleted.  Before changing the foreign key, the migration
checks that every non-null legacy auth user id maps to public.users.auth_user_id.
It aborts without writing if any row cannot be preserved.
"""

from django.db import migrations


FORWARD_SQL = """
DO $$
DECLARE
    unmapped_count integer;
BEGIN
    SELECT count(*) INTO unmapped_count
    FROM public.doc_patients p
    LEFT JOIN public.users u ON u.auth_user_id = p.user_id
    WHERE p.user_id IS NOT NULL AND u.id IS NULL;

    IF unmapped_count <> 0 THEN
        RAISE EXCEPTION
            'Cannot move doc_patients.user_id: % linked profile(s) have no public.users mapping',
            unmapped_count;
    END IF;

    ALTER TABLE public.doc_patients
        DROP CONSTRAINT IF EXISTS doc_patients_user_id_fkey;

    UPDATE public.doc_patients p
       SET user_id = u.id
      FROM public.users u
     WHERE p.user_id = u.auth_user_id;

    ALTER TABLE public.doc_patients
        ADD CONSTRAINT doc_patients_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0004_register_existing_patient_tables")]

    operations = [
        migrations.RunSQL(FORWARD_SQL, reverse_sql=migrations.RunSQL.noop),
    ]
