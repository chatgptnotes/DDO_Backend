"""
Process DPDP automated data deletion requests.

Implements the automated deletion workflow for patient data retention
according to DPDP Act 2023 requirements.

Usage:
    python manage.py process_dpdp_deletions --dry-run        # Preview what would be deleted
    python manage.py process_dpdp_deletions --once          # Process one batch and exit
    python manage.py process_dpdp_deletions                 # Run continuously (daemon mode)

The command:
1. Finds patients whose data has exceeded retention periods
2. Creates deletion requests for eligible patients
3. Executes deletions in safe priority order
4. Logs all actions for audit compliance
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import timedelta
from typing import ClassVar

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from surgeonpilot.models import (
    DpdpDeletionAudit,
    DpdpDeletionRequest,
    DpdpRetentionRule,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process DPDP automated data deletion requests."

    # Personal data field names for each table (for audit logging)
    PERSONAL_DATA_FIELDS: ClassVar[dict[str, list[str]]] = {
        "doc_patients": [
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "date_of_birth",
            "profile_image_url",
        ],
        "doc_patient_addresses": [
            "address_line_1",
            "address_line_2",
            "city",
            "state",
            "postal_code",
            "country",
        ],
        "doc_patient_emergency_contacts": [
            "contact_name",
            "phone_number",
            "email",
        ],
        "doc_patient_allergies": [
            "allergy_name",
            "reaction_description",
        ],
        "doc_patient_medications": [
            "medication_name",
            "prescribing_doctor",
        ],
        "doc_patient_medical_history": [
            "condition_name",
            "notes",
        ],
        "doc_patient_insurance": [
            "provider_name",
            "policy_number",
            "policy_holder_name",
        ],
        "doc_appointments": [
            "patient_name",
            "patient_email",
            "patient_phone",
            "notes",
        ],
        "doc_consultation_notes": [
            "chief_complaint",
            "history_of_present_illness",
            "examination_findings",
            "diagnosis",
            "treatment_plan",
            "follow_up_instructions",
            "additional_notes",
        ],
        "doc_prescriptions": [
            "diagnosis",
            "notes",
        ],
        "doc_patient_reports": [
            "file_name",
            "description",
        ],
        "doc_patient_intake_forms": [
            "form_data",
        ],
    }

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview deletions without executing them",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process one batch and exit",
        )
        parser.add_argument(
            "--poll-seconds",
            type=float,
            default=30.0,
            help="Seconds to wait between batches in daemon mode (default: 30)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10,
            help="Maximum deletion requests to process per batch (default: 10)",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.batch_size = options["batch_size"]

        if self.dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No actual deletions will occur"))

        while True:
            try:
                # Process pending deletion requests
                processed = self._process_pending_requests()

                # Scan for patients eligible for retention-based deletion
                if not self.dry_run:
                    self._scan_retention_eligible_patients()

                if options["once"]:
                    if processed == 0:
                        self.stdout.write("No pending deletion requests to process.")
                    return

                if processed == 0:
                    # No work to do, wait before next poll
                    time.sleep(max(options["poll_seconds"], 0.1))
                else:
                    # Process next batch immediately
                    continue

            except Exception as exc:
                logger.exception("Error processing deletion requests: %s", exc)
                self.stderr.write(self.style.ERROR(f"Error: {exc}"))
                if options["once"]:
                    raise
                time.sleep(max(options["poll_seconds"], 0.1))

    def _process_pending_requests(self) -> int:
        """Process pending deletion requests."""
        with transaction.atomic():
            # Claim next batch of pending requests
            requests = list(
                DpdpDeletionRequest.objects.filter(status="pending")
                .select_for_update(skip_locked=True)
                .order_by("created_at")[: self.batch_size]
            )

            if not requests:
                return 0

            for req in requests:
                self._process_request(req)

        return len(requests)

    def _process_request(self, req: DpdpDeletionRequest) -> None:
        """Process a single deletion request."""
        self.stdout.write(f"Processing deletion request {req.reference_number}")

        if self.dry_run:
            self._preview_deletion(req)
            return

        with transaction.atomic():
            # Mark as in progress
            req.status = "in_progress"
            req.started_at = timezone.now()
            req.save(update_fields=["status", "started_at"])

            try:
                summary = self._execute_deletion(req)

                req.status = "completed"
                req.completed_at = timezone.now()
                req.deletion_summary = summary
                req.save(update_fields=["status", "completed_at", "deletion_summary"])

                self.stdout.write(
                    self.style.SUCCESS(
                        f"Completed deletion request {req.reference_number}: "
                        f"{summary.get('total_records_deleted', 0)} records deleted"
                    )
                )

            except Exception as exc:
                req.status = "failed"
                req.completed_at = timezone.now()
                req.error_message = str(exc)[:1000]
                req.save(update_fields=["status", "completed_at", "error_message"])

                self.stderr.write(
                    self.style.ERROR(
                        f"Failed deletion request {req.reference_number}: {exc}"
                    )
                )
                raise

    def _preview_deletion(self, req: DpdpDeletionRequest) -> None:
        """Preview what would be deleted for a request (dry-run mode)."""
        summary = {"tables": {}}

        for table_name in req.tables_to_delete:
            rule = DpdpRetentionRule.objects.filter(
                table_name=table_name, is_active=True
            ).first()

            if not rule or rule.retention_years is None:
                summary["tables"][table_name] = {"status": "skipped (no rule or indefinite retention)"}
                continue

            count = self._count_eligible_records(req.patient_id, table_name)
            summary["tables"][table_name] = {"records_to_delete": count}

        self.stdout.write(self.style.WARNING(f"Preview for {req.reference_number}:"))
        self.stdout.write(f"  Patient ID: {req.patient_id}")
        self.stdout.write(f"  Patient Email: {req.patient_email}")
        for table, info in summary["tables"].items():
            self.stdout.write(f"  {table}: {info}")

    def _execute_deletion(self, req: DpdpDeletionRequest) -> dict:
        """Execute the actual deletion for a request."""
        summary = {
            "total_records_deleted": 0,
            "tables_deleted": [],
            "errors": [],
        }

        # Get retention rules for tables in priority order
        rules = (
            DpdpRetentionRule.objects.filter(
                table_name__in=req.tables_to_delete, is_active=True
            )
            .exclude(retention_years__isnull=True)
            .order_by("priority")
        )

        deleted_record_ids = set()

        for rule in rules:
            try:
                with transaction.atomic():
                    # Delete records and audit
                    deleted_ids = self._delete_table_records(
                        req=req, rule=rule, deleted_so_far=deleted_record_ids
                    )
                    deleted_record_ids.update(deleted_ids)

                    if deleted_ids:
                        summary["tables_deleted"].append(rule.table_name)
                        summary["total_records_deleted"] += len(deleted_ids)

            except Exception as exc:
                error_msg = f"{rule.table_name}: {exc}"
                summary["errors"].append(error_msg)
                logger.error("Deletion error for %s: %s", rule.table_name, exc)

        return summary

    def _delete_table_records(
        self, req: DpdpDeletionRequest, rule: DpdpRetentionRule, deleted_so_far: set
    ) -> set[str]:
        """Delete records from a specific table and audit the action."""
        deleted_ids = set()

        # Get eligible record IDs
        record_ids = self._get_eligible_record_ids(req.patient_id, rule.table_name)

        if not record_ids:
            return deleted_ids

        # Prepare personal fields list for audit
        personal_fields = self.PERSONAL_DATA_FIELDS.get(rule.table_name, [])

        # Delete each record individually (for audit)
        for record_id in record_ids:
            with connection.cursor() as cur:
                # Execute deletion
                cur.execute(
                    f"""
                    DELETE FROM {rule.table_name}
                    WHERE id = %s
                    RETURNING id
                    """,
                    [record_id],
                )

                if cur.rowcount > 0:
                    deleted_ids.add(str(record_id))

                    # Log audit entry (without personal data)
                    self._log_deletion_audit(
                        deletion_request_id=req.id,
                        table_name=rule.table_name,
                        record_id=record_id,
                        deleted_fields=personal_fields,
                        patient_email=req.patient_email,
                    )

        return deleted_ids

    def _get_eligible_record_ids(self, patient_id: uuid.UUID | None, table_name: str) -> list[uuid.UUID]:
        """Get IDs of records eligible for deletion from a table."""
        with connection.cursor() as cur:
            if table_name == "doc_patients":
                # For patient table, check retention based on last activity
                cur.execute(
                    """
                    SELECT id FROM doc_patients
                    WHERE id = %s
                    AND NOT patient_has_active_data(id)
                    AND created_at < NOW() - INTERVAL '7 years'
                    FOR UPDATE SKIP LOCKED
                    """,
                    [patient_id],
                )
            elif table_name in ("doc_appointments", "doc_consultation_notes", "doc_prescriptions"):
                # Medical records tables with patient_id foreign key
                cur.execute(
                    f"""
                    SELECT id FROM {table_name}
                    WHERE patient_id = %s
                    AND created_at < NOW() - INTERVAL '7 years'
                    FOR UPDATE SKIP LOCKED
                    """,
                    [patient_id],
                )
            elif table_name.startswith("doc_patient_"):
                # Patient detail tables with patient_id foreign key
                cur.execute(
                    f"""
                    SELECT id FROM {table_name}
                    WHERE patient_id = %s
                    AND created_at < NOW() - INTERVAL '7 years'
                    FOR UPDATE SKIP LOCKED
                    """,
                    [patient_id],
                )
            else:
                # Generic case (should not happen with seeded rules)
                cur.execute(
                    f"""
                    SELECT id FROM {table_name}
                    WHERE patient_id = %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    [patient_id],
                )

            return [row[0] for row in cur.fetchall()]

    def _count_eligible_records(self, patient_id: uuid.UUID | None, table_name: str) -> int:
        """Count records eligible for deletion (for dry-run preview)."""
        with connection.cursor() as cur:
            if table_name == "doc_patients":
                cur.execute(
                    """
                    SELECT COUNT(*) FROM doc_patients
                    WHERE id = %s
                    AND NOT patient_has_active_data(id)
                    AND created_at < NOW() - INTERVAL '7 years'
                    """,
                    [patient_id],
                )
            elif table_name in ("doc_appointments", "doc_consultation_notes", "doc_prescriptions"):
                cur.execute(
                    f"""
                    SELECT COUNT(*) FROM {table_name}
                    WHERE patient_id = %s
                    AND created_at < NOW() - INTERVAL '7 years'
                    """,
                    [patient_id],
                )
            elif table_name.startswith("doc_patient_"):
                cur.execute(
                    f"""
                    SELECT COUNT(*) FROM {table_name}
                    WHERE patient_id = %s
                    AND created_at < NOW() - INTERVAL '7 years'
                    """,
                    [patient_id],
                )
            else:
                cur.execute(
                    f"""
                    SELECT COUNT(*) FROM {table_name}
                    WHERE patient_id = %s
                    """,
                    [patient_id],
                )

            return cur.fetchone()[0]

    def _log_deletion_audit(
        self,
        deletion_request_id: uuid.UUID,
        table_name: str,
        record_id: uuid.UUID,
        deleted_fields: list[str],
        patient_email: str | None,
    ) -> None:
        """Log an audit entry for a deletion (without storing personal data)."""
        with connection.cursor() as cur:
            # Generate summary without personal data
            summary = f"Record from {table_name}"
            if patient_email:
                # Store only that email was present, not the value
                summary += f" (patient email on file)"

            cur.execute(
                """
                INSERT INTO doc_dpdp_deletion_audit (
                    deletion_request_id,
                    table_name,
                    record_id,
                    record_type,
                    deleted_fields,
                    record_summary,
                    executed_by,
                    status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    deletion_request_id,
                    table_name,
                    record_id,
                    table_name.replace("doc_", ""),
                    deleted_fields,
                    summary,
                    "system",
                    "deleted",
                ],
            )

    def _scan_retention_eligible_patients(self) -> None:
        """Scan for patients eligible for retention-based deletion and create requests."""
        with connection.cursor() as cur:
            # Find patients with no activity in 7+ years and no active grievances
            cur.execute(
                """
                INSERT INTO doc_dpdp_deletion_requests (
                    id,
                    patient_id,
                    patient_email,
                    request_type,
                    reason,
                    status
                )
                SELECT
                    gen_random_uuid(),
                    p.id,
                    p.email,
                    'auto_retention',
                    'Patient data exceeds 7-year retention period with no active data',
                    'pending'
                FROM doc_patients p
                WHERE p.created_at < NOW() - INTERVAL '7 years'
                AND NOT patient_has_active_data(p.id)
                AND NOT EXISTS (
                    SELECT 1 FROM doc_dpdp_deletion_requests
                    WHERE patient_id = p.id
                    AND status IN ('pending', 'in_progress')
                )
                ON CONFLICT (patient_id, status) DO NOTHING
                RETURNING id, reference_number, patient_email
                LIMIT 10
                """
            )

            created = cur.fetchall()
            if created:
                for req_id, ref_num, email in created:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Created auto-retention deletion request {ref_num} for patient {email}"
                        )
                    )
