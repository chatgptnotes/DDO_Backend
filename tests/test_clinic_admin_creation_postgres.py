from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from core.models import LocalUser, UserRole
from surgeonpilot.models import Doctor


class ClinicAdminCreationPostgresTests(TransactionTestCase):
    """Regression tests for the local PostgreSQL Clinic Admin creation path."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.schema_editor() as editor:
            editor.create_model(Doctor)
            editor.create_model(UserRole)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(UserRole)
            editor.delete_model(Doctor)
        super().tearDownClass()

    def setUp(self):
        self.client = APIClient()
        self.superadmin = LocalUser.objects.create_user(
            email="superadmin@example.com", password="SuperAdminPassword9!", role="admin"
        )
        Doctor.objects.create(
            id="11111111-1111-1111-1111-111111111111",
            user_id=self.superadmin.id,
            email=self.superadmin.email,
            full_name="Super Admin",
            role="superadmin",
            consultation_type="medical",
            is_active=True,
        )
        self.payload = {
            "fullName": "Clinic Manager",
            "email": "clinic.manager@example.com",
            "organizationName": "Example Clinic",
            "designation": "Clinic Manager",
            "department": "Operations",
            "address": "1 Example Street",
            "city": "Mumbai",
            "state": "Maharashtra",
            "pincode": "400001",
            "password": "ClinicManagerPassword9!",
        }

    def post(self, **changes):
        self.client.force_authenticate(user=self.superadmin)
        return self.client.post("/api/surgeon/clinic-admins/", {**self.payload, **changes}, format="json")

    def test_success_creates_local_user_profile_and_canonical_role(self):
        response = self.post()

        self.assertEqual(response.status_code, 201)
        user = LocalUser.objects.get(email=self.payload["email"])
        doctor = Doctor.objects.get(user_id=user.id)
        self.assertTrue(user.check_password(self.payload["password"]))
        self.assertEqual(doctor.role, "admin_clinical")  # legacy portal compatibility
        self.assertEqual(doctor.clinic_name, self.payload["organizationName"])
        self.assertEqual(doctor.clinic_address, self.payload["address"])
        self.assertEqual(doctor.city, self.payload["city"])
        self.assertEqual(doctor.state, self.payload["state"])
        self.assertEqual(doctor.pincode, self.payload["pincode"])
        self.assertEqual(doctor.designation, self.payload["designation"])
        self.assertEqual(doctor.department, self.payload["department"])
        self.assertIsNone(doctor.clinic_id)
        self.assertEqual(
            UserRole.objects.filter(
                user=user, role="clinical_admin", scope_id__isnull=True, is_active=True
            ).count(),
            1,
        )

    def test_unauthenticated_request_is_rejected(self):
        response = APIClient().post("/api/surgeon/clinic-admins/", self.payload, format="json")
        self.assertIn(response.status_code, (401, 403))

    def test_duplicate_normalized_email_is_rejected(self):
        LocalUser.objects.create_user(
            email="CLINIC.MANAGER@example.com", password="ExistingPassword9!", role="admin"
        )
        response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data.get("error", {}))

    def test_weak_password_is_rejected(self):
        response = self.post(password="password")
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data)

    def test_role_failure_rolls_back_user_and_profile(self):
        self.client.raise_request_exception = False
        with patch("surgeonpilot.clinic_admin.UserRole.objects.create", side_effect=RuntimeError("role insert failed")):
            response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertFalse(LocalUser.objects.filter(email=self.payload["email"]).exists())
        self.assertFalse(Doctor.objects.filter(email=self.payload["email"]).exists())

    def test_creation_never_queries_supabase_auth_or_clinics_tables(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.post()
        self.assertEqual(response.status_code, 201)
        sql = "\n".join(query["sql"].lower() for query in queries.captured_queries)
        self.assertNotIn("auth.users", sql)
        self.assertNotIn("auth.identities", sql)
        self.assertNotIn("public.clinics", sql)
