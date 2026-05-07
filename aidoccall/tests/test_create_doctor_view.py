"""
Tests for `POST /api/aidoccall/admin/doctors/` (CreateDoctorView).

The view delegates the SQL + Supabase Auth Admin work to
`aidoccall.services.doctors.create_doctor_profile`. We mock that boundary so
these tests exercise auth, permissions, and serializer validation without
needing a real Postgres connection.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from aidoccall.services.doctors import DoctorCreationResult


URL = "/api/aidoccall/admin/doctors/"


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def stub_create_doctor(monkeypatch):
    """Replace the create_doctor_profile service with a recording stub."""
    calls: list[dict] = []

    def _stub(*, caller_user_id, payload, scope_id=None):
        calls.append(
            {"caller_user_id": caller_user_id, "payload": dict(payload), "scope_id": scope_id}
        )
        return DoctorCreationResult(
            user_id="00000000-0000-0000-0000-000000000001",
            email=payload["email"].lower(),
            was_new_user=False,
            role_granted=True,
            profile_created=True,
        )

    monkeypatch.setattr("aidoccall.views.create_doctor_profile", _stub)
    return calls


@pytest.mark.django_db
def test_requires_authentication(api_client):
    response = api_client.post(URL, data={})
    assert response.status_code == 401


@pytest.mark.django_db
def test_blocks_non_admin(api_client, make_token, patch_roles, stub_create_doctor):
    patch_roles("doc-1", ["doctor"])
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='doc-1')}")
    response = api_client.post(
        URL,
        data={"email": "x@y.com", "full_name": "X"},
        format="json",
    )
    assert response.status_code == 403
    assert stub_create_doctor == []


@pytest.mark.django_db
def test_clinical_admin_can_create(api_client, make_token, patch_roles, stub_create_doctor):
    patch_roles("admin-1", ["clinical_admin"])
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='admin-1')}")
    response = api_client.post(
        URL,
        data={
            "email": "Dr.Anita@x.com",
            "full_name": "Dr Anita",
            "phone": "+919999999999",
            "specialization": "Cardiology",
            "consultation_fee": "800.00",
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["user_id"] == "00000000-0000-0000-0000-000000000001"
    assert body["role_granted"] is True
    assert stub_create_doctor[0]["caller_user_id"] == "admin-1"
    # Email is forwarded as-typed; the service is responsible for lowercasing.
    assert stub_create_doctor[0]["payload"]["email"] == "Dr.Anita@x.com"
    assert stub_create_doctor[0]["payload"]["consultation_fee"] == Decimal("800.00")


@pytest.mark.django_db
def test_superadmin_can_create(api_client, make_token, patch_roles, stub_create_doctor):
    patch_roles("super-1", ["superadmin"])
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='super-1')}")
    response = api_client.post(
        URL,
        data={"email": "x@y.com", "full_name": "X"},
        format="json",
    )
    assert response.status_code == 201
    assert len(stub_create_doctor) == 1


@pytest.mark.django_db
def test_rejects_invalid_email(api_client, make_token, patch_roles, stub_create_doctor):
    patch_roles("admin-1", ["clinical_admin"])
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='admin-1')}")
    response = api_client.post(
        URL,
        data={"email": "not-an-email", "full_name": "X"},
        format="json",
    )
    assert response.status_code == 400
    assert "email" in response.json()
    assert stub_create_doctor == []


@pytest.mark.django_db
def test_rejects_missing_full_name(api_client, make_token, patch_roles, stub_create_doctor):
    patch_roles("admin-1", ["clinical_admin"])
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='admin-1')}")
    response = api_client.post(URL, data={"email": "x@y.com"}, format="json")
    assert response.status_code == 400
    assert "full_name" in response.json()


@pytest.mark.django_db
def test_existing_user_path_is_success(api_client, make_token, patch_roles, monkeypatch):
    """The original 'email already exists' bug — now must succeed."""
    patch_roles("admin-1", ["clinical_admin"])

    def _stub(*, caller_user_id, payload, scope_id=None):
        return DoctorCreationResult(
            user_id="user-already-existed",
            email=payload["email"].lower(),
            was_new_user=False,  # email matched an existing auth user
            role_granted=True,
            profile_created=True,
        )

    monkeypatch.setattr("aidoccall.views.create_doctor_profile", _stub)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='admin-1')}")
    response = api_client.post(
        URL,
        data={"email": "admin@x.com", "full_name": "Admin"},
        format="json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["was_new_user"] is False
    assert body["role_granted"] is True


@pytest.mark.django_db
def test_supabase_failure_returns_502(api_client, make_token, patch_roles, monkeypatch):
    from core.supabase_admin import SupabaseAdminError

    patch_roles("admin-1", ["clinical_admin"])

    def _boom(*, caller_user_id, payload, scope_id=None):
        raise SupabaseAdminError(status=500, body="upstream down")

    monkeypatch.setattr("aidoccall.views.create_doctor_profile", _boom)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='admin-1')}")
    response = api_client.post(
        URL,
        data={"email": "x@y.com", "full_name": "X"},
        format="json",
    )
    assert response.status_code == 502
