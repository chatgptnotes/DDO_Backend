"""
Snapshot test for /api/aidoccall/patient/selected-doctors/.

USAGE
=====

1. Log into aidoccall.com in your browser as a patient who has at least one
   selected doctor.
2. Pull the access token (DevTools → Application → Local Storage → look for
   `sb-<project-ref>-auth-token`, parse the JSON, copy `access_token`).
3. Run:

       export SUPABASE_JWT='eyJhbGc...'
       export SUPABASE_PROJECT_REF='uakqdjxuceckjssjdyui'
       export SUPABASE_ANON_KEY='<aidoccall.com VITE_SUPABASE_ANON_KEY>'
       python scripts/compare_selected_doctors.py

Expected output: ✅ Identical response.

Note: this endpoint derives `patient_id` from the JWT — Django ignores any
URL `patient_id` param. Supabase REST uses an explicit filter, so the script
decodes the JWT, looks up the matching `doc_patients.id` via Supabase, and
filters with that.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


REQUIRED_ENV = ("SUPABASE_JWT", "SUPABASE_PROJECT_REF", "SUPABASE_ANON_KEY")


def _require_env() -> dict[str, str]:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(f"❌ missing env vars: {', '.join(missing)}")
    return {k: os.environ[k] for k in REQUIRED_ENV}


def _http_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "null")


def _decode_jwt_sub(token: str) -> str:
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return payload["sub"]


def _resolve_patient_id(env: dict[str, str], user_id: str) -> str:
    """Look up the doc_patients.id for the given auth user_id, via Supabase REST."""
    qs = urllib.parse.urlencode({"select": "id", "user_id": f"eq.{user_id}", "limit": "1"})
    url = f"https://{env['SUPABASE_PROJECT_REF']}.supabase.co/rest/v1/doc_patients?{qs}"
    code, body = _http_json(
        url,
        {
            "apikey": env["SUPABASE_ANON_KEY"],
            "Authorization": f"Bearer {env['SUPABASE_JWT']}",
            "Accept": "application/json",
        },
    )
    if code != 200 or not body:
        sys.exit(f"❌ could not find doc_patients row for user {user_id}: {code} {body!r}")
    return body[0]["id"]


def fetch_via_supabase(env: dict[str, str], patient_id: str) -> Any:
    select = (
        "*,"
        "doctor:doctor_id("
        "id,full_name,specialization,clinic_name,clinic_address,"
        "experience_years,consultation_fee,online_fee,is_verified"
        ")"
    )
    qs = urllib.parse.urlencode(
        {
            "select": select,
            "patient_id": f"eq.{patient_id}",
            "order": "is_favorite.desc",
        }
    )
    url = f"https://{env['SUPABASE_PROJECT_REF']}.supabase.co/rest/v1/doc_patient_doctor_selections?{qs}"
    code, body = _http_json(
        url,
        {
            "apikey": env["SUPABASE_ANON_KEY"],
            "Authorization": f"Bearer {env['SUPABASE_JWT']}",
            "Accept": "application/json",
        },
    )
    if code != 200:
        sys.exit(f"❌ Supabase returned {code}: {body!r}")
    return body


def fetch_via_django(env: dict[str, str]) -> Any:
    backend = os.environ.get("DJANGO_BACKEND_URL", "http://localhost:8000")
    url = f"{backend.rstrip('/')}/api/aidoccall/patient/selected-doctors/"
    code, body = _http_json(
        url,
        {
            "Authorization": f"Bearer {env['SUPABASE_JWT']}",
            "Accept": "application/json",
        },
    )
    if code != 200:
        sys.exit(f"❌ Django returned {code}: {body!r}")
    return body


def main() -> None:
    env = _require_env()
    user_id = _decode_jwt_sub(env["SUPABASE_JWT"])
    print(f"  user_id (sub): {user_id}")
    patient_id = _resolve_patient_id(env, user_id)
    print(f"  patient_id:    {patient_id}")

    sb = fetch_via_supabase(env, patient_id)
    dj = fetch_via_django(env)

    if sb == dj:
        print(f"✅ Identical response — {len(dj)} rows")
        return

    print("❌ Responses differ.")
    if isinstance(sb, list) and isinstance(dj, list):
        if len(sb) != len(dj):
            print(f"  row count: supabase={len(sb)} django={len(dj)}")
        for i, (a, b) in enumerate(zip(sb, dj)):
            if a != b:
                for k in set(a) | set(b):
                    if a.get(k) != b.get(k):
                        print(f"  row {i} field {k!r}: supabase={a.get(k)!r} django={b.get(k)!r}")
                break
    sys.exit(1)


if __name__ == "__main__":
    main()
