"""
Tests for `core.supabase_admin.find_user_by_email`.

Pin the client-side email-match behavior: GoTrue's `?email=` filter is
unreliable across versions and may return the first page of users
regardless. We must verify the email matches before returning a hit.
Failing this check would cause the wrong identity to be granted the doctor
role.
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import pytest

from core import supabase_admin


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.SUPABASE_URL = "https://example.supabase.co"
    settings.SUPABASE_SERVICE_ROLE_KEY = "test-service-key"


def _fake_response(payload: dict | list):
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return _Resp()


def test_returns_none_when_api_returns_unrelated_user():
    """The actual production bug — GoTrue ignored ?email= and returned someone else."""
    with patch.object(supabase_admin.request, "urlopen") as mock:
        mock.return_value = _fake_response(
            {"users": [{"id": "other-uuid", "email": "someone-else@x.com"}]}
        )
        result = supabase_admin.find_user_by_email("target@x.com")
    assert result is None


def test_returns_user_when_email_matches():
    with patch.object(supabase_admin.request, "urlopen") as mock:
        mock.return_value = _fake_response(
            {"users": [{"id": "u-1", "email": "Target@X.com"}]}
        )
        result = supabase_admin.find_user_by_email("target@x.com")
    assert result is not None
    assert result.id == "u-1"
    assert result.email == "target@x.com"  # lowercased


def test_handles_list_response_shape():
    with patch.object(supabase_admin.request, "urlopen") as mock:
        mock.return_value = _fake_response(
            [{"id": "u-1", "email": "target@x.com"}]
        )
        result = supabase_admin.find_user_by_email("target@x.com")
    assert result is not None
    assert result.id == "u-1"


def test_handles_data_envelope():
    with patch.object(supabase_admin.request, "urlopen") as mock:
        mock.return_value = _fake_response(
            {"data": [{"id": "u-1", "email": "target@x.com"}]}
        )
        result = supabase_admin.find_user_by_email("target@x.com")
    assert result is not None


def test_finds_match_in_later_position_on_unfiltered_page():
    """When the API ignores the filter, we still find a match by scanning."""
    with patch.object(supabase_admin.request, "urlopen") as mock:
        mock.return_value = _fake_response(
            {
                "users": [
                    {"id": "a", "email": "noise@x.com"},
                    {"id": "b", "email": "more-noise@x.com"},
                    {"id": "target-id", "email": "target@x.com"},
                ]
            }
        )
        result = supabase_admin.find_user_by_email("target@x.com")
    assert result is not None
    assert result.id == "target-id"


def test_empty_email_returns_none():
    """Don't even call the API for empty input."""
    with patch.object(supabase_admin.request, "urlopen") as mock:
        result = supabase_admin.find_user_by_email("")
    assert result is None
    assert mock.call_count == 0


def test_missing_credentials_raises():
    from django.test.utils import override_settings

    with override_settings(SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY=""):
        with pytest.raises(supabase_admin.SupabaseAdminError):
            supabase_admin.find_user_by_email("x@y.com")
