# Remap `LocalUser` onto the shared `public.users` table.
#
# The model state changes (UUID pk, email login, no AbstractUser columns) are
# STATE-ONLY. The database changes happen as explicit SQL because Django's
# autodetector would otherwise try to RENAME core_localuser -> users, which
# collides with the already-existing `users` table. Instead we:
#   1. add the two columns `users` lacks (last_login, is_indian_resident);
#   2. copy every core_localuser row into `users` (new UUID ids; legacy rows
#      whose email already exists keep their original uuid via the crosswalk);
#   3. retarget core_patientprofile.user_id from core_localuser(bigint) to
#      users(uuid), preserving its row;
#   4. drop core_localuser and its auth m2m join tables.
# Everything is idempotent and guarded so it also works on a fresh database
# (no core_localuser yet) or one where `users` does not exist yet.

import core.models
import uuid
from django.db import migrations, models


STATE_OPERATIONS = [
    migrations.AlterModelManagers(
        name='localuser',
        managers=[
            ('objects', core.models.LocalUserManager()),
        ],
    ),
    migrations.RemoveField(
        model_name='localuser',
        name='date_joined',
    ),
    migrations.RemoveField(
        model_name='localuser',
        name='first_name',
    ),
    migrations.RemoveField(
        model_name='localuser',
        name='groups',
    ),
    migrations.RemoveField(
        model_name='localuser',
        name='is_active',
    ),
    migrations.RemoveField(
        model_name='localuser',
        name='is_staff',
    ),
    migrations.RemoveField(
        model_name='localuser',
        name='is_superuser',
    ),
    migrations.RemoveField(
        model_name='localuser',
        name='last_name',
    ),
    migrations.RemoveField(
        model_name='localuser',
        name='user_permissions',
    ),
    migrations.RemoveField(
        model_name='localuser',
        name='username',
    ),
    migrations.AddField(
        model_name='localuser',
        name='auth_user_id',
        field=models.UUIDField(blank=True, null=True),
    ),
    migrations.AlterField(
        model_name='localuser',
        name='email',
        field=models.EmailField(blank=True, max_length=254, null=True, unique=True),
    ),
    migrations.AlterField(
        model_name='localuser',
        name='id',
        field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
    ),
    migrations.AlterField(
        model_name='localuser',
        name='is_indian_resident',
        field=models.BooleanField(blank=True, null=True),
    ),
    migrations.AlterField(
        model_name='localuser',
        name='password',
        field=models.TextField(blank=True, null=True),
    ),
    migrations.AlterModelTable(
        name='localuser',
        table='users',
    ),
]

# Kept in sync with the legacy schema so fresh databases get the same shape.
CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
    email text UNIQUE,
    full_name text,
    role text DEFAULT 'user',
    phone text,
    password text,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    employee_id varchar(255),
    shift_timing varchar(255),
    specialization_focus varchar(255),
    supervisor_id uuid REFERENCES users(id) ON DELETE SET NULL,
    hire_date date DEFAULT CURRENT_DATE,
    department varchar(255) DEFAULT 'Customer Support',
    salary_range varchar(255),
    performance_rating numeric DEFAULT 0.00
);
"""

# The auth.users FK only exists where the Supabase auth schema exists; apply
# it opportunistically so fresh databases match the legacy shape.
AUTH_USERS_FK_SQL = """
DO $$
BEGIN
    IF to_regclass('auth.users') IS NOT NULL THEN
        EXECUTE $fk$
            ALTER TABLE users ADD CONSTRAINT users_auth_user_id_fkey
            FOREIGN KEY (auth_user_id) REFERENCES auth.users(id) ON DELETE CASCADE
        $fk$;
    END IF;
EXCEPTION WHEN duplicate_object THEN NULL;  -- constraint already there
END $$;
"""

MIGRATE_DATA_SQL = """
DO $$
BEGIN
    -- Columns Django's auth machinery and the registration flow need.
    ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login timestamptz;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS is_indian_resident boolean;

    IF to_regclass('public.core_localuser') IS NOT NULL THEN
        -- Crosswalk: old bigint id -> uuid. If a legacy `users` row already
        -- has the same email, reuse ITS uuid so FKs stay consistent.
        CREATE TEMP TABLE _clu_xwalk AS
        SELECT l.id AS old_id,
               COALESCE(
                   (SELECT u.id FROM users u WHERE u.email = l.email),
                   gen_random_uuid()
               ) AS new_id
        FROM core_localuser l;

        INSERT INTO users (id, auth_user_id, email, full_name, role, phone,
                           password, is_indian_resident, created_at, updated_at)
        SELECT x.new_id, NULL, l.email, l.full_name, l.role, l.phone,
               l.password, l.is_indian_resident, l.created_at, l.updated_at
        FROM core_localuser l
        JOIN _clu_xwalk x ON x.old_id = l.id
        WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.email = l.email)
        ON CONFLICT (id) DO NOTHING;

        IF to_regclass('public.core_patientprofile') IS NOT NULL THEN
            ALTER TABLE core_patientprofile
                DROP CONSTRAINT IF EXISTS core_patientprofile_user_id_776323f1_fk_core_localuser_id;

            -- bigint -> uuid without USING-subquery tricks: new column, fill
            -- by join, swap, restore OneToOne uniqueness + NOT NULL.
            ALTER TABLE core_patientprofile ADD COLUMN user_id_new uuid;
            UPDATE core_patientprofile p
               SET user_id_new = x.new_id
              FROM _clu_xwalk x
             WHERE x.old_id = p.user_id;
            ALTER TABLE core_patientprofile DROP COLUMN user_id;
            ALTER TABLE core_patientprofile RENAME COLUMN user_id_new TO user_id;
            ALTER TABLE core_patientprofile ALTER COLUMN user_id SET NOT NULL;
            ALTER TABLE core_patientprofile ADD UNIQUE (user_id);
            ALTER TABLE core_patientprofile
                ADD CONSTRAINT core_patientprofile_user_fk
                FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
        END IF;

        -- `users` fully replaces the Django-defaulted table. CASCADE drops
        -- the FKs pointing at core_localuser but NOT the m2m join tables
        -- themselves, so drop those explicitly.
        DROP TABLE IF EXISTS core_localuser CASCADE;
        DROP TABLE IF EXISTS core_localuser_groups;
        DROP TABLE IF EXISTS core_localuser_user_permissions;
    END IF;
END $$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_localuser_is_indian_resident'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=STATE_OPERATIONS,
            database_operations=[
                migrations.RunSQL(CREATE_USERS_SQL, elidable=True),
                migrations.RunSQL(AUTH_USERS_FK_SQL, elidable=True),
                migrations.RunSQL(MIGRATE_DATA_SQL, elidable=True),
            ],
        ),
    ]
