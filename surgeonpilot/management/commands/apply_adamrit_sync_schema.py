from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Apply the idempotent Adamrit sync-job schema migration."

    def handle(self, *args, **options):
        migration = (
            settings.BASE_DIR
            / "supabase"
            / "migrations"
            / "20260805_create_adamrit_sync_jobs.sql"
        )
        sql = Path(migration).read_text(encoding="utf-8")
        with connection.cursor() as cursor:
            cursor.execute(sql)
        self.stdout.write(self.style.SUCCESS("Adamrit sync-job schema is ready."))
