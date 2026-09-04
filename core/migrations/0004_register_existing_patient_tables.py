"""Register existing PostgreSQL patient tables in Django's migration state.

The live schema was verified before this migration was added. This migration is
state-only: it has no database operations and therefore cannot create, alter,
or delete any database table or row.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0003_user_to_users_table")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="DocPatient",
                    fields=[
                        ("id", models.UUIDField(editable=False, primary_key=True, serialize=False)),
                        ("email", models.EmailField(blank=True, max_length=254, null=True)),
                        ("first_name", models.TextField(blank=True, null=True)),
                        ("last_name", models.TextField(blank=True, null=True)),
                        ("phone_number", models.TextField(blank=True, null=True)),
                        ("date_of_birth", models.DateField(blank=True, null=True)),
                        ("gender", models.TextField(blank=True, null=True)),
                        ("blood_group", models.TextField(blank=True, null=True)),
                        ("address", models.TextField(blank=True, null=True)),
                        ("registration_step", models.IntegerField(blank=True, null=True)),
                        ("registration_completed", models.BooleanField(blank=True, null=True)),
                        ("is_indian_resident", models.BooleanField(blank=True, null=True)),
                        ("intake_form_completed", models.BooleanField(blank=True, null=True)),
                        ("created_at", models.DateTimeField(blank=True, null=True)),
                        ("updated_at", models.DateTimeField(blank=True, null=True)),
                        ("user", models.ForeignKey(blank=True, db_column="user_id", db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="doc_patient_profiles", to="core.localuser")),
                    ],
                    options={"managed": False, "db_table": "doc_patients"},
                ),
                migrations.CreateModel(
                    name="UserRole",
                    fields=[
                        ("id", models.UUIDField(editable=False, primary_key=True, serialize=False)),
                        ("role", models.CharField(max_length=50)),
                        ("scope_id", models.UUIDField(blank=True, null=True)),
                        ("is_active", models.BooleanField(default=True)),
                        ("granted_at", models.DateTimeField(auto_now_add=True)),
                        ("granted_by", models.UUIDField(blank=True, null=True)),
                        ("revoked_at", models.DateTimeField(blank=True, null=True)),
                        ("revoked_by", models.UUIDField(blank=True, null=True)),
                        ("user", models.ForeignKey(db_column="user_id", db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="role_assignments", to="core.localuser")),
                    ],
                    options={"managed": False, "db_table": "user_roles"},
                ),
            ],
            database_operations=[],
        )
    ]
