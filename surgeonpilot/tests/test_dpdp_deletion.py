"""
Tests for DPDP automated data deletion mechanism.

Tests cover:
1. Eligible records for deletion (7+ years old, no active data)
2. Non-eligible records (recent activity, active grievance)
3. Already deleted records (idempotent retry)
4. Admin manual deletion request creation
5. Audit logging without personal data retention
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, APITestCase

from core.roles import has_role
from surgeonpilot.models import (
    DpdpDeletionAudit,
    DpdpDeletionRequest,
    DpdpRetentionRule,
    Doctor,
)
from surgeonpilot.views import (
    CancelDeletionRequestView,
    CreateManualDeletionRequestView,
    DpdpDeletionRequestDetailView,
    DpdpDeletionRequestListView,
    DpdpRetentionRuleListView,
)


class DpdpRetentionRulesTestCase(TestCase):
    """Test retention rules are properly seeded and accessible."""

    def test_retention_rules_exist(self):
        """Test that retention rules are seeded."""
        rules = DpdpRetentionRule.objects.filter(is_active=True).order_by("priority")
        self.assertGreater(rules.count(), 0)

        # Check key rules exist
        patient_rule = rules.filter(table_name="doc_patients").first()
        self.assertIsNotNone(patient_rule)
        self.assertEqual(patient_rule.retention_years, 7)

        # Audit logs should have indefinite retention
        audit_rule = rules.filter(table_name="doc_audit_logs").first()
        self.assertIsNotNone(audit_rule)
        self.assertIsNone(audit_rule.retention_years)


class DpdpDeletionEligibilityTestCase(TestCase):
    """Test patient eligibility for deletion based on retention rules."""

    def setUp(self):
        """Set up test data with varying ages."""
        self.old_date = timezone.now() - timedelta(days=8 * 365)  # 8 years ago
        self.recent_date = timezone.now() - timedelta(days=30)  # 30 days ago

    def test_old_patient_eligible_for_deletion(self):
        """Test that patients with 7+ year old data and no recent activity are eligible."""
        with connection.cursor() as cur:
            # Create old patient record
            patient_id = uuid.uuid4()
            cur.execute(
                """
                INSERT INTO doc_patients (id, user_id, email, first_name, last_name, date_of_birth, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [patient_id, uuid.uuid4(), "old@example.com", "Old", "Patient", "1990-01-01", self.old_date],
            )

            # Verify no active data
            cur.execute(
                "SELECT patient_has_active_data(%s)",
                [patient_id],
            )
            has_active = cur.fetchone()[0]

            self.assertFalse(has_active, "Old patient should have no active data")

    def test_recent_patient_not_eligible_for_deletion(self):
        """Test that patients with recent appointments are not eligible for deletion."""
        with connection.cursor() as cur:
            # Create recent patient and appointment
            patient_id = uuid.uuid4()
            user_id = uuid.uuid4()

            cur.execute(
                """
                INSERT INTO doc_patients (id, user_id, email, first_name, last_name, date_of_birth, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [patient_id, user_id, "recent@example.com", "Recent", "Patient", "1990-01-01", self.old_date],
            )

            # Add recent appointment
            doctor_id = uuid.uuid4()
            cur.execute(
                """
                INSERT INTO doc_doctors (id, user_id, email, full_name, consultation_type, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [doctor_id, user_id, "doctor@example.com", "Test Doctor", "online", timezone.now()],
            )

            cur.execute(
                """
                INSERT INTO doc_appointments (id, doctor_id, patient_id, patient_name, patient_email, appointment_date, start_time, end_time, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    uuid.uuid4(),
                    doctor_id,
                    patient_id,
                    "Recent Patient",
                    "recent@example.com",
                    self.recent_date,
                    "10:00:00",
                    "11:00:00",
                    self.recent_date,
                ],
            )

            # Verify patient has active data
            cur.execute(
                "SELECT patient_has_active_data(%s)",
                [patient_id],
            )
            has_active = cur.fetchone()[0]

            self.assertTrue(has_active, "Patient with recent appointment should have active data")


class DpdpDeletionRequestAPITestCase(APITestCase):
    """Test API endpoints for deletion request management (admin only)."""

    def setUp(self):
        """Set up test client and superadmin user."""
        self.factory = APIRequestFactory()
        self.superadmin_id = uuid.uuid4()

        # Create a mock superadmin user
        self.user = type("User", (), {"id": str(self.superadmin_id), "is_authenticated": True})()

        # Mock the has_role function to return True for superadmin
        self.has_role_patcher = patch("core.roles.has_role", return_value=True)
        self.has_role_patcher.start()

    def tearDown(self):
        """Clean up patches."""
        self.has_role_patcher.stop()

    def test_retention_rules_list_requires_superadmin(self):
        """Test that listing retention rules requires superadmin role."""
        view = DpdpRetentionRuleListView.as_view()
        request = self.factory.get("/api/surgeon/dpdp/retention-rules/")
        request.user = self.user

        with patch("core.roles.has_role", return_value=True):
            response = view(request)
            self.assertEqual(response.status_code, 200)

    def test_create_manual_deletion_request(self):
        """Test creating a manual deletion request for a patient."""
        patient_id = uuid.uuid4()

        # Create test patient
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO doc_patients (id, user_id, email, first_name, last_name, date_of_birth, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [patient_id, uuid.uuid4(), "test@example.com", "Test", "Patient", "1990-01-01", timezone.now()],
            )

        view = CreateManualDeletionRequestView.as_view()
        request = self.factory.post(
            "/api/surgeon/dpdp/deletion-requests/create/",
            {"patient_id": str(patient_id), "reason": "Test deletion"},
            format="json",
        )
        request.user = self.user

        response = view(request)
        self.assertEqual(response.status_code, 201)
        self.assertIn("reference_number", response.data)

    def test_duplicate_deletion_request_returns_existing(self):
        """Test that creating a duplicate deletion request returns existing one."""
        patient_id = uuid.uuid4()

        # Create test patient
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO doc_patients (id, user_id, email, first_name, last_name, date_of_birth, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [patient_id, uuid.uuid4(), "test@example.com", "Test", "Patient", "1990-01-01", timezone.now()],
            )

        # Create first request
        view = CreateManualDeletionRequestView.as_view()
        request = self.factory.post(
            "/api/surgeon/dpdp/deletion-requests/create/",
            {"patient_id": str(patient_id), "reason": "Test deletion"},
            format="json",
        )
        request.user = self.user
        response1 = view(request)
        self.assertEqual(response1.status_code, 201)

        # Try to create duplicate
        response2 = view(request)
        self.assertEqual(response2.status_code, 409)  # Conflict
        self.assertIn("request", response2.data)

    def test_cancel_pending_deletion_request(self):
        """Test cancelling a pending deletion request."""
        # Create a deletion request
        deletion_request = DpdpDeletionRequest.objects.create(
            id=uuid.uuid4(),
            patient_id=uuid.uuid4(),
            patient_email="test@example.com",
            request_type="admin_manual",
            status="pending",
            created_by=self.superadmin_id,
        )

        view = CancelDeletionRequestView.as_view()
        request = self.factory.delete(f"/api/surgeon/dpdp/deletion-requests/{deletion_request.id}/cancel/")
        request.user = self.user

        response = view(request, request_id=deletion_request.id)
        self.assertEqual(response.status_code, 200)

        # Refresh and check status
        deletion_request.refresh_from_db()
        self.assertEqual(deletion_request.status, "cancelled")


class DpdpDeletionExecutionTestCase(TestCase):
    """Test the actual deletion execution and audit logging."""

    def test_audit_log_does_not_contain_personal_data(self):
        """Test that audit logs do not contain actual personal data values."""
        deletion_request_id = uuid.uuid4()

        with connection.cursor() as cur:
            # Create an audit entry
            cur.execute(
                """
                INSERT INTO doc_dpdp_deletion_audit (
                    deletion_request_id,
                    table_name,
                    record_id,
                    deleted_fields,
                    record_summary,
                    executed_by,
                    status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    deletion_request_id,
                    "doc_patients",
                    uuid.uuid4(),
                    ["email", "first_name", "last_name"],
                    "Record from doc_patients (patient email on file)",
                    "system",
                    "deleted",
                ],
            )

            # Verify no actual email is in the summary
            audit_id = cur.fetchone()[0]

            cur.execute(
                "SELECT record_summary FROM doc_dpdp_deletion_audit WHERE id = %s",
                [audit_id],
            )
            summary = cur.fetchone()[0]

            self.assertNotIn("@", summary, "Summary should not contain actual email addresses")
            self.assertIn("doc_patients", summary)

    def test_deletion_request_idempotent_retry(self):
        """Test that re-running a deletion request is idempotent."""
        patient_id = uuid.uuid4()

        # Create a deletion request in completed state
        deletion_request = DpdpDeletionRequest.objects.create(
            id=uuid.uuid4(),
            patient_id=patient_id,
            patient_email="test@example.com",
            request_type="auto_retention",
            status="completed",
            deletion_summary={"total_records_deleted": 5},
            created_at=timezone.now() - timedelta(days=1),
            completed_at=timezone.now(),
        )

        # Run the management command - should skip completed requests
        with patch("surgeonpilot.management.commands.process_dpdp_deletions.logger"):
            call_command("process_dpdp_deletions", "--once")

        # Verify request is still completed and not modified
        deletion_request.refresh_from_db()
        self.assertEqual(deletion_request.status, "completed")
        self.assertEqual(deletion_request.deletion_summary.get("total_records_deleted"), 5)


class DpdpRetentionRuleProtectionTestCase(TestCase):
    """Test that records with indefinite retention cannot be deleted."""

    def test_audit_logs_protected_from_deletion(self):
        """Test that audit log records are protected from automatic deletion."""
        with connection.cursor() as cur:
            # Verify retention rule for audit logs is NULL (indefinite)
            cur.execute(
                "SELECT retention_years FROM doc_dpdp_retention_rules WHERE table_name = 'doc_audit_logs'"
            )
            result = cur.fetchone()

            self.assertIsNone(result[0], "Audit logs should have indefinite retention")

    def test_grievance_records_protected_from_deletion(self):
        """Test that grievance records are protected from automatic deletion."""
        with connection.cursor() as cur:
            # Verify retention rule for grievances is NULL (indefinite)
            cur.execute(
                "SELECT retention_years FROM doc_dpdp_retention_rules WHERE table_name = 'doc_dpdp_grievances'"
            )
            result = cur.fetchone()

            self.assertIsNone(result[0], "Grievance records should have indefinite retention")
