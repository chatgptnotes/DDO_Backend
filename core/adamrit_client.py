"""Adamrit ecosystem bridge — read-only client for the hospital's HMS.

Adamrit (adamrit.com) exposes read-only views over its Supabase REST API for
the ecosystem apps: ecosystem_patients, ecosystem_visits (and
ecosystem_rmo_doctors / ecosystem_receivables for the other sister apps).
This client anchors DDO's ABDM records to real Adamrit patients instead of
duplicating a patient master here.

Stdlib-only (urllib) so it adds no dependency. The default key is Adamrit's
public anon key — the same one Adamrit's own frontend ships. Override both
via environment for other environments:

    ADAMRIT_SUPABASE_URL, ADAMRIT_ANON_KEY
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

ADAMRIT_URL = os.environ.get("ADAMRIT_SUPABASE_URL", "https://xvkxccqaopbnkvwgyfjv.supabase.co")
ADAMRIT_ANON_KEY = os.environ.get("ADAMRIT_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inh2a3hjY3Fhb3Bibmt2d2d5Zmp2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDc4MjMwMTIsImV4cCI6MjA2MzM5OTAxMn0.z9UkKHDm4RPMs_2IIzEPEYzd3-sbQSF6XpxaQg3vZhU")

_TIMEOUT = 15


class AdamritError(RuntimeError):
    """Adamrit's API was unreachable or answered with an error."""


def _get(view: str, params: dict[str, str]) -> list[dict]:
    query = urllib.parse.urlencode({"select": "*", **params})
    req = urllib.request.Request(
        f"{ADAMRIT_URL}/rest/v1/{view}?{query}",
        headers={"apikey": ADAMRIT_ANON_KEY, "Authorization": f"Bearer {ADAMRIT_ANON_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as res:
            return json.load(res)
    except Exception as exc:  # noqa: BLE001 - one bridge, one error type
        raise AdamritError(f"Adamrit {view} unavailable: {exc}") from exc


def search_patients(term: str, limit: int = 25) -> list[dict]:
    """Patients by name or patient code (e.g. 'UHAY25...')."""
    safe = term.replace("%", " ").replace(",", " ").replace("(", " ").replace(")", " ").strip()
    if not safe:
        return []
    return _get(
        "ecosystem_patients",
        {
            "or": f"(name.ilike.*{safe}*,patient_code.ilike.*{safe}*)",
            "limit": str(limit),
            "order": "name",
        },
    )


def get_patient(patient_id: str) -> dict | None:
    rows = _get("ecosystem_patients", {"id": f"eq.{patient_id}", "limit": "1"})
    return rows[0] if rows else None


def get_visits(patient_id: str, limit: int = 50) -> list[dict]:
    """The patient's Adamrit visits, newest first."""
    return _get(
        "ecosystem_visits",
        {"patient_id": f"eq.{patient_id}", "order": "visit_date.desc", "limit": str(limit)},
    )
