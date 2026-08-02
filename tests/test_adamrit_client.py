"""The Adamrit bridge fails loudly, not with half-answers."""
from unittest import mock

import pytest

from core import adamrit_client


def test_search_patients_blank_term_returns_empty_without_calling_out():
    with mock.patch.object(adamrit_client, "_get") as get:
        assert adamrit_client.search_patients("   ") == []
        get.assert_not_called()


def test_unreachable_adamrit_raises_adamrit_error():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
        with pytest.raises(adamrit_client.AdamritError):
            adamrit_client.get_patient("00000000-0000-0000-0000-000000000000")


def test_search_strips_postgrest_delimiters():
    with mock.patch.object(adamrit_client, "_get", return_value=[]) as get:
        adamrit_client.search_patients("kumar,(test)%")
        params = get.call_args[0][1]
        assert "%" not in params["or"].replace("*", "")
        assert "(name.ilike" in params["or"]
