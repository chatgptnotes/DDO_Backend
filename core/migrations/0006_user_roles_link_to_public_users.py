"""Move user_roles.user_id from legacy auth IDs to public.users IDs.

Existing role rows are preserved. The migration aborts if any role cannot be
mapped through public.users.auth_user_id before it changes the foreign key.
"""

from django.db import migrations


FORWARD_SQL = """
DO $$
DECLARE
    unmapped_count integer;
BEGIN
    SELECT count(*) INTO unmapped_count
    FROM public.user_roles r
    LEFT JOIN public.users u ON u.auth_user_id = r.user_id
    WHERE u.id IS NULL;

    IF unmapped_count <> 0 THEN
        RAISE EXCEPTION
            'Cannot move user_roles.user_id: % role(s) have no public.users mapping',
            unmapped_count;
    END IF;

    ALTER TABLE public.user_roles
        DROP CONSTRAINT IF EXISTS user_roles_user_id_fkey;

    UPDATE public.user_roles r
       SET user_id = u.id
      FROM public.users u
     WHERE r.user_id = u.auth_user_id;

    ALTER TABLE public.user_roles
        ADD CONSTRAINT user_roles_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0005_doc_patients_link_to_public_users")]

    operations = [
        migrations.RunSQL(FORWARD_SQL, reverse_sql=migrations.RunSQL.noop),
    ]
