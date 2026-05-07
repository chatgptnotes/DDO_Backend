"""
Snapshot test: compare the Django /api/surgeon/clinic/doctors/ response against
the existing Supabase REST response for the same authenticated user.

USAGE
=====

1. Log into AiSurgeonPilot in your browser as a clinical_admin or superadmin.
2. Open DevTools → Application → Cookies. Find a cookie that looks like
       sb-<project-ref>-auth-token
   Its value is a base64-encoded JSON. The `access_token` field inside is
   the Supabase JWT we need.

   Easier path: in DevTools console, run:
       (await window.supabase.auth.getSession()).data.session.access_token
   …if `window.supabase` is exposed. Otherwise, copy the long string after
   `"access_token":"` from the cookie value.

3. Export it:
       export SUPABASE_JWT='eyJhbGciOi...'
       export SUPABASE_PROJECT_REF='uakqdjxuceckjssjdyui'   # from .env DATABASE_URL
       export SUPABASE_ANON_KEY='<anon key from your AiSurgeonPilot .env>'
       export DJANGO_BACKEND_URL='http://localhost:8000'

4. Run:
       python scripts/compare_clinic_doctors.py

The script prints either ✅ Identical response or a diff.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any


REQUIRED_ENV = ("SUPABASE_JWT", "SUPABASE_PROJECT_REF", "SUPABASE_ANON_KEY")


def _require_env() -> dict[str, str]:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(f"❌ missing env vars: {', '.join(missing)}\n   See the docstring at the top of this file.")
    return {k: os.environ[k] for k in REQUIRED_ENV}


def _http_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "null")


def _decode_jwt_sub(token: str) -> str:
    """Pull the `sub` (user id) out of a JWT without verifying — for the filter."""
    import base64

    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)  # pad
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return payload["sub"]


def fetch_via_supabase(env: dict[str, str], user_id: str) -> Any:
    """Replicate the Branding page's exact Supabase REST call."""
    base = f"https://{env['SUPABASE_PROJECT_REF']}.supabase.co/rest/v1/doc_doctors"
    qs = urllib.parse.urlencode(
        {
            "select": "*",
            "role": f"eq.doctor",
            "created_by": f"eq.{user_id}",
            "order": "full_name.asc",
        }
    )
    url = f"{base}?{qs}"
    headers = {
        "apikey": env["SUPABASE_ANON_KEY"],
        "Authorization": f"Bearer {env['SUPABASE_JWT']}",
        "Accept": "application/json",
    }
    code, body = _http_json(url, headers)
    if code != 200:
        sys.exit(f"❌ Supabase returned {code}: {body!r}")
    return body


def fetch_via_django(env: dict[str, str]) -> Any:
    backend = os.environ.get("DJANGO_BACKEND_URL", "http://localhost:8000")
    url = f"{backend.rstrip('/')}/api/surgeon/clinic/doctors/"
    headers = {
        "Authorization": f"Bearer {env['SUPABASE_JWT']}",
        "Accept": "application/json",
    }
    code, body = _http_json(url, headers)
    if code != 200:
        sys.exit(f"❌ Django returned {code}: {body!r}")
    return body


def diff(label_a: str, a: Any, label_b: str, b: Any) -> None:
    if a == b:
        print(f"✅ Identical response — {len(a) if isinstance(a, list) else 'n/a'} rows")
        return

    print(f"❌ Responses differ.\n")
    if isinstance(a, list) and isinstance(b, list) and len(a) != len(b):
        print(f"  row count: {label_a}={len(a)} vs {label_b}={len(b)}")

    if isinstance(a, list) and isinstance(b, list):
        for i, (ra, rb) in enumerate(zip(a, b)):
            if ra != rb:
                ka, kb = set(ra), set(rb)
                only_a = ka - kb
                only_b = kb - ka
                if only_a:
                    print(f"  row {i}: keys only in {label_a}: {sorted(only_a)}")
                if only_b:
                    print(f"  row {i}: keys only in {label_b}: {sorted(only_b)}")
                for k in ka & kb:
                    if ra[k] != rb[k]:
                        print(f"  row {i} field {k!r}: {label_a}={ra[k]!r} vs {label_b}={rb[k]!r}")
                break  # one row of detail is enough
    sys.exit(1)


def main() -> None:
    env = _require_env()
    user_id = _decode_jwt_sub(env["SUPABASE_JWT"])
    print(f"  using user_id (sub): {user_id}")

    print("  fetching from Supabase REST…")
    sb = fetch_via_supabase(env, user_id)

    print("  fetching from Django backend…")
    dj = fetch_via_django(env)

    diff("supabase", sb, "django", dj)


if __name__ == "__main__":
    main()
