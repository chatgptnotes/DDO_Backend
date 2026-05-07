"""
Verify Supabase JWT authentication on a protected endpoint.

We use /api/me/ (IsAuthenticated) as the canary because it's the simplest
authenticated endpoint and exercises the full auth pipeline.
"""
import pytest


@pytest.mark.django_db
def test_me_requires_authorization_header(client):
    response = client.get("/api/me/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_me_rejects_non_bearer_scheme(client, make_token):
    token = make_token()
    response = client.get("/api/me/", HTTP_AUTHORIZATION=f"Basic {token}")
    assert response.status_code == 401


@pytest.mark.django_db
def test_me_rejects_invalid_signature(client, make_token):
    token = make_token(secret="totally-wrong-secret")
    response = client.get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 401
    assert "invalid" in response.json()["detail"].lower()


@pytest.mark.django_db
def test_me_rejects_expired_token(client, make_token):
    token = make_token(exp_offset=-60)
    response = client.get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


@pytest.mark.django_db
def test_me_rejects_wrong_audience(client, make_token):
    token = make_token(audience="not-authenticated")
    response = client.get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 401


@pytest.mark.django_db
def test_me_returns_user_info_for_valid_token(client, auth_header, patch_roles):
    patch_roles("user-1", ["doctor", "patient"])
    response = client.get("/api/me/", **auth_header(sub="user-1", email="doc@x.com"))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "user-1"
    assert body["email"] == "doc@x.com"
    assert set(body["roles"]) == {"doctor", "patient"}
    assert body["active_role"] in body["roles"]


@pytest.mark.django_db
def test_me_returns_empty_roles_when_user_has_none(client, auth_header, patch_roles):
    patch_roles("user-1", [])
    response = client.get("/api/me/", **auth_header(sub="user-1"))
    assert response.status_code == 200
    body = response.json()
    assert body["roles"] == []
    assert body["active_role"] is None


@pytest.mark.django_db
def test_me_honours_active_role_header_when_user_has_it(client, auth_header, patch_roles):
    patch_roles("user-1", ["doctor", "patient"])
    headers = auth_header(sub="user-1")
    headers["HTTP_X_ACTIVE_ROLE"] = "patient"
    response = client.get("/api/me/", **headers)
    assert response.status_code == 200
    assert response.json()["active_role"] == "patient"


@pytest.mark.django_db
def test_me_ignores_active_role_header_when_user_lacks_it(client, auth_header, patch_roles):
    patch_roles("user-1", ["doctor"])
    headers = auth_header(sub="user-1")
    headers["HTTP_X_ACTIVE_ROLE"] = "superadmin"
    response = client.get("/api/me/", **headers)
    assert response.status_code == 200
    # Active role falls back to first held role, not the requested-but-unheld role.
    assert response.json()["active_role"] == "doctor"
