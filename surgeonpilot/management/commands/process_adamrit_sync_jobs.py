from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from scripts.pull_adamrit_murali import sync_adamrit_murali
from surgeonpilot.models import AdamritSyncJob


class Command(BaseCommand):
    help = "Process queued Adamrit-to-DDO sync jobs."

    def add_arguments(self, parser):
        parser.add_argument("--poll-seconds", type=float, default=2.0)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        while True:
            job = self._claim_next_job()
            if job:
                self._run_job(job)
            elif options["once"]:
                return
            else:
                time.sleep(max(options["poll_seconds"], 0.1))

    @staticmethod
    def _claim_next_job() -> AdamritSyncJob | None:
        with transaction.atomic():
            job = (
                AdamritSyncJob.objects.select_for_update(skip_locked=True)
                .filter(status="queued")
                .order_by("created_at")
                .first()
            )
            if not job:
                return None
            job.status = "running"
            job.started_at = timezone.now()
            job.save(update_fields=["status", "started_at"])
            return job

    def _run_job(self, job: AdamritSyncJob) -> None:
        self.stdout.write(f"Running Adamrit sync job {job.id}")
        try:
            result = sync_adamrit_murali()
            job.status = "succeeded"
            job.result_summary = {
                "source_counts": result["source_counts"],
                "target_counts": result["target_counts"],
                "notes": result["notes"],
            }
            job.error_message = None
        except Exception as exc:  # noqa: BLE001 - the job must persist its failure state
            job.status = "failed"
            job.result_summary = None
            job.error_message = str(exc)[:1000]
            self.stderr.write(f"Adamrit sync job {job.id} failed: {exc}")
        finally:
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "result_summary", "error_message", "finished_at"])
