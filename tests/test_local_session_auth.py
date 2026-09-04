from unittest.mock import patch

from django.test import Client, TestCase

from core.models import LocalUser


class LocalSessionAuthenticationTests(TestCase):
    def setUp(self):
        self.user = LocalUser.objects.create_user(
            email="patient.session@example.com",
            password="SafePatientPassword9!",
            full_name="Session Patient",
            role="patient",
        )
        self.client = Client(enforce_csrf_checks=True)

    def _csrf_token(self):
        response = self.client.get("/api/auth/csrf/")
        self.assertEqual(response.status_code, 200)
        return response.cookies["csrftoken"].value

    def _login(self, password="SafePatientPassword9!"):
        return self.client.post(
            "/api/auth/login/",
            {"email": self.user.email, "password": password},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self._csrf_token(),
        )

    def test_postgresql_user_can_login_and_read_me(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

        with patch("core.views.roles_module.list_roles", return_value=["patient"]):
            me = self.client.get("/api/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], self.user.email)
        self.assertEqual(me.json()["roles"], ["patient"])
        self.assertEqual(me.json()["active_role"], "patient")

    def test_invalid_password_is_rejected(self):
        response = self._login("wrong-password")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["message"], "Invalid email or password")

    def test_login_requires_csrf_token(self):
        response = self.client.post(
            "/api/auth/login/",
            {"email": self.user.email, "password": "SafePatientPassword9!"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_logout_invalidates_the_session(self):
        self.assertEqual(self._login().status_code, 200)
        response = self.client.post(
            "/api/auth/logout/", HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/me/").status_code, 403)
