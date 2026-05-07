"""
Role-based authorization on a real DRF view.

We register a throwaway view that requires HasRole('doctor') and verify
the permission gate behaves correctly without needing a real Postgres DB
(roles are stubbed via the patch_roles fixture).
"""
from __future__ import annotations

from django.urls import path
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.views import APIView

from core.permissions import HasRole


class DoctorOnlyView(APIView):
    permission_classes = [IsAuthenticated, HasRole("doctor")]

    def get(self, request):
        return Response({"ok": True})


class AdminOrSuperView(APIView):
    permission_classes = [IsAuthenticated, HasRole("clinical_admin", "superadmin")]

    def get(self, request):
        return Response({"ok": True})


urlpatterns = [
    path("test-doctor-only/", DoctorOnlyView.as_view()),
    path("test-admin-or-super/", AdminOrSuperView.as_view()),
]


import pytest


@pytest.fixture
def api_client(settings):
    settings.ROOT_URLCONF = __name__
    return APIClient()


@pytest.mark.django_db
def test_doctor_only_allows_doctor(api_client, make_token, patch_roles):
    patch_roles("doc-1", ["doctor"])
    token = make_token(sub="doc-1")
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = api_client.get("/test-doctor-only/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_doctor_only_blocks_patient(api_client, make_token, patch_roles):
    patch_roles("pat-1", ["patient"])
    token = make_token(sub="pat-1")
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = api_client.get("/test-doctor-only/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_doctor_only_blocks_user_with_no_roles(api_client, make_token, patch_roles):
    patch_roles("u-1", [])
    token = make_token(sub="u-1")
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = api_client.get("/test-doctor-only/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_doctor_only_requires_authentication(api_client):
    response = api_client.get("/test-doctor-only/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_admin_or_super_allows_either(api_client, make_token, patch_roles):
    patch_roles("u-1", ["clinical_admin"])
    token = make_token(sub="u-1")
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert api_client.get("/test-admin-or-super/").status_code == 200

    patch_roles("u-2", ["superadmin"])
    token = make_token(sub="u-2")
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert api_client.get("/test-admin-or-super/").status_code == 200


@pytest.mark.django_db
def test_doctor_who_is_also_patient_passes_doctor_gate(api_client, make_token, patch_roles):
    """The whole point: one user can hold both roles."""
    patch_roles("u-1", ["doctor", "patient"])
    token = make_token(sub="u-1")
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = api_client.get("/test-doctor-only/")
    assert response.status_code == 200
