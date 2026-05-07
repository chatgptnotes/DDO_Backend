"""Health endpoint is unauthenticated and always returns 200."""
import pytest


@pytest.mark.django_db
def test_health_returns_200_without_auth(client):
    response = client.get("/api/health/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "service" in body
    assert "version" in body


@pytest.mark.django_db
def test_health_ignores_invalid_token(client):
    response = client.get("/api/health/", HTTP_AUTHORIZATION="Bearer not-a-token")
    assert response.status_code == 200
