"""
Tests for POST /api/surgeon/transcribe/.

The transcription engine (ffmpeg + network) is monkeypatched out — these tests
exercise the view's auth gate, validation, and response shaping only.
"""
from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from surgeonpilot.services.transcription import TranscriptionError, TranscriptionResult

URL = "/api/surgeon/transcribe/"


@pytest.fixture
def api_client():
    return APIClient()


def _audio_file(content: bytes = b"fake-audio-bytes", name: str = "consult.webm"):
    return SimpleUploadedFile(name, content, content_type="audio/webm")


@pytest.mark.django_db
def test_requires_authentication(api_client):
    response = api_client.post(URL, {"audio": _audio_file()}, format="multipart")
    assert response.status_code == 401


@pytest.mark.django_db
def test_any_authenticated_user_allowed(api_client, make_token, monkeypatch):
    """The endpoint is gated on authentication only (stateless STT utility), so
    any signed-in user passes the permission gate. (Role-gating can be restored
    later via HasRole — see the view.)"""
    monkeypatch.setattr(
        "surgeonpilot.views.transcribe_audio",
        lambda **_: TranscriptionResult(transcript="ok", language="hi-IN", engine="google"),
    )
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='anyone')}")
    response = api_client.post(URL, {"audio": _audio_file()}, format="multipart")
    assert response.status_code == 200


@pytest.mark.django_db
def test_missing_file_returns_400(api_client, make_token, patch_roles):
    patch_roles("doc-1", ["doctor"])
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='doc-1')}")
    response = api_client.post(URL, {"language": "hi-IN"}, format="multipart")
    assert response.status_code == 400
    assert "audio" in response.json()["detail"].lower()


@pytest.mark.django_db
def test_empty_file_returns_400(api_client, make_token, patch_roles):
    patch_roles("doc-1", ["doctor"])
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='doc-1')}")
    response = api_client.post(
        URL, {"audio": _audio_file(content=b"")}, format="multipart"
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_success_returns_transcript(api_client, make_token, patch_roles, monkeypatch):
    patch_roles("doc-1", ["doctor"])

    captured = {}

    def fake_transcribe(*, audio_bytes, filename, language):
        captured["language"] = language
        captured["bytes"] = audio_bytes
        return TranscriptionResult(
            transcript="नमस्ते डॉक्टर", language=language, engine="google"
        )

    monkeypatch.setattr("surgeonpilot.views.transcribe_audio", fake_transcribe)

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='doc-1')}")
    response = api_client.post(
        URL, {"audio": _audio_file(), "language": "hi-IN"}, format="multipart"
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "transcript": "नमस्ते डॉक्टर",
        "language": "hi-IN",
        "engine": "google",
    }
    assert captured["language"] == "hi-IN"
    assert captured["bytes"] == b"fake-audio-bytes"


@pytest.mark.django_db
def test_defaults_to_hindi_when_language_omitted(
    api_client, make_token, patch_roles, monkeypatch
):
    patch_roles("doc-1", ["doctor"])
    seen = {}

    def fake_transcribe(*, audio_bytes, filename, language):
        seen["language"] = language
        return TranscriptionResult(transcript="", language=language, engine="google")

    monkeypatch.setattr("surgeonpilot.views.transcribe_audio", fake_transcribe)

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='doc-1')}")
    response = api_client.post(URL, {"audio": _audio_file()}, format="multipart")

    assert response.status_code == 200
    assert seen["language"] == "hi-IN"


@pytest.mark.django_db
def test_engine_failure_returns_502(api_client, make_token, patch_roles, monkeypatch):
    patch_roles("doc-1", ["doctor"])

    def boom(*, audio_bytes, filename, language):
        raise TranscriptionError("service unavailable")

    monkeypatch.setattr("surgeonpilot.views.transcribe_audio", boom)

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(sub='doc-1')}")
    response = api_client.post(URL, {"audio": _audio_file()}, format="multipart")

    assert response.status_code == 502
    assert response.json()["detail"] == "service unavailable"
