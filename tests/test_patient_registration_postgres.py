from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from core.models import DocPatient, LocalUser, UserRole


class PatientRegistrationPostgresTests(TransactionTestCase):
    """Regression tests for the PostgreSQL-only patient registration path."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.schema_editor() as editor:
            editor.create_model(DocPatient)
            editor.create_model(UserRole)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(UserRole)
            editor.delete_model(DocPatient)
        super().tearDownClass()

    def setUp(self):
        self.client = APIClient()
        self.payload = {
            "email": "new.patient@example.com",
            "password": "SafePatientPassword9!",
            "full_name": "New Patient",
            "is_indian_resident": True,
        }

    def post(self, **changes):
        return self.client.post(
            "/api/patients/register/", {**self.payload, **changes}, format="json"
        )

    def test_successful_registration_creates_user_profile_and_role(self):
        response = self.post()

        self.assertEqual(response.status_code, 201)
        user = LocalUser.objects.get(email="new.patient@example.com")
        self.assertTrue(user.check_password(self.payload["password"]))
        self.assertEqual(user.role, "patient")
        profile = DocPatient.objects.get(email="new.patient@example.com")
        self.assertEqual(profile.user_id, user.id)
        self.assertEqual(UserRole.objects.filter(user=user, role="patient", is_active=True).count(), 1)

    def test_existing_unlinked_patient_profile_is_linked_without_duplicate(self):
        existing = DocPatient.objects.create(
            email="existing.patient@example.com", first_name="Existing"
        )

        response = self.post(email="existing.patient@example.com")

        self.assertEqual(response.status_code, 201)
        existing.refresh_from_db()
        self.assertIsNotNone(existing.user_id)
        self.assertEqual(DocPatient.objects.filter(email__iexact=existing.email).count(), 1)

    def test_duplicate_email_is_rejected(self):
        LocalUser.objects.create_user(
            email="new.patient@example.com", password="ExistingPassword9!", role="patient"
        )

        response = self.post()

        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data["errors"])

    def test_weak_password_is_rejected_by_django_validators(self):
        response = self.post(password="password")

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data["errors"])

    def test_role_failure_rolls_back_user_and_profile(self):
        self.client.raise_request_exception = False
        with patch("core.serializers.UserRole.objects.create", side_effect=RuntimeError("role insert failed")):
            response = self.post()

        self.assertEqual(response.status_code, 500)
        self.assertFalse(LocalUser.objects.filter(email="new.patient@example.com").exists())
        self.assertFalse(DocPatient.objects.filter(email="new.patient@example.com").exists())

    def test_registration_never_queries_auth_schema(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.post()

        self.assertEqual(response.status_code, 201)
        sql = "\n".join(query["sql"].lower() for query in queries.captured_queries)
        self.assertNotIn("auth.users", sql)
        self.assertNotIn("auth.identities", sql)
