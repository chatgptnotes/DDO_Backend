# Backend (Django REST API)

Shared Django REST Framework backend for **AiSurgeonPilot** and **aidoccall.com**.

- **Auth**: Supabase Auth issues JWTs. This backend only **verifies** them. No passwords ever touch this service.
- **Data**: Connects to the same Supabase Postgres both frontends already use.
- **Multi-role users**: One Supabase user can be doctor + clinical_admin + patient via the `user_roles` join table.
- **Production-safe rollout**: New features land here first; existing endpoints migrate behind frontend feature flags one at a time.

---

## Architecture

```
Frontend (Next.js / Vite)        Supabase Auth        This backend (Django)        Postgres (Supabase)
                                                     
  Login / signup / MFA  ────────► Supabase                                       
                       ◄──────── JWT (access + refresh)                          
                                                     
  Business request                                                               
  Authorization: Bearer <jwt> ──────────────────────► Verify signature           
                                                      Look up roles              
                                                      Run business logic ──────► SELECT/INSERT
                                                                          ◄──── rows
                                                      Return JSON               
                  ◄────────────────────────────────                              
```

---

## Local setup

1. Create and activate a virtualenv:
   ```bash
   cd backend
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy env template and fill in real values:
   ```bash
   cp .env.example .env
   ```
   Required values:
   - `DJANGO_SECRET_KEY` — any long random string for dev.
   - `DATABASE_URL` — your **staging** Supabase Postgres connection string (NOT prod).
     Get it from Supabase: *Project Settings → Database → Connection string → URI*.
   - `SUPABASE_JWT_SECRET` — *Project Settings → API → JWT Secret*.

3. Apply the `user_roles` migration in your Supabase staging project:
   - Open the Supabase SQL editor for the staging project.
   - Paste the contents of `supabase/migrations/20260501_create_user_roles.sql`.
   - Run it. Verify with `SELECT role, count(*) FROM public.user_roles GROUP BY role;`.

4. Run the dev server:
   ```bash
   python manage.py runserver
   ```
   Hit `http://localhost:8000/api/health/` — should return `{"status":"ok",...}`.

5. Run tests:
   ```bash
   pytest
   ```

---

## What's here

| Path | Purpose |
|---|---|
| `config/` | Django project (settings, urls, wsgi/asgi). |
| `config/settings/base.py` | Settings shared by all envs. |
| `config/settings/dev.py` | Local dev — connects to staging Supabase Postgres or sqlite fallback. |
| `config/settings/prod.py` | Production / staging deploy. |
| `config/settings/test.py` | Used by pytest. SQLite in-memory; deterministic JWT secret. |
| `core/authentication.py` | `SupabaseJWTAuthentication` — verifies the Bearer JWT. |
| `core/permissions.py` | `HasRole("doctor", ...)` — role gate using `user_roles`. |
| `core/roles.py` | `list_roles()`, `has_role()`, `get_active_role()`. |
| `core/views.py` | `/api/health/`, `/api/me/`. |
| `surgeonpilot/` | App for AiSurgeonPilot endpoints. URL prefix: `/api/surgeon/`. |
| `aidoccall/` | App for aidoccall.com endpoints. URL prefix: `/api/aidoccall/`. |
| `supabase/migrations/` | SQL migrations applied via the Supabase SQL editor (NOT Django migrations). |
| `tests/` | pytest suite — auth, permissions, health. |

---

## Frontend integration

### AiSurgeonPilot (Next.js, `@supabase/ssr`)

```ts
// AiSurgeonPilot/src/lib/api-client.ts (new file)
import { createBrowserClient } from "@supabase/ssr";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL!; // e.g. https://api.example.com

export async function apiFetch(path: string, init: RequestInit = {}) {
  const supabase = createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
  );
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token;

  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${BACKEND_URL}${path}`, { ...init, headers });
  if (res.status === 401) {
    // Token expired. Supabase SDK auto-refreshes on next call; just retry.
  }
  return res;
}
```

### aidoccall.com (Vite/React, `@supabase/supabase-js`)

```js
// aidoccall.com/src/lib/apiClient.js (new file)
import { supabase } from "./supabaseClient";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL;

export async function apiFetch(path, init = {}) {
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token;

  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  return fetch(`${BACKEND_URL}${path}`, { ...init, headers });
}
```

The frontend keeps using Supabase exactly as it does today for **login/signup/MFA/password reset**. The only change is data calls move from `supabase.from('table').select()` to `apiFetch('/api/...')`.

---

## Migration playbook (per existing endpoint)

Use this for every Supabase-direct call that gets moved to Django:

1. Build the Django view. Match the response shape **byte-for-byte**.
2. Add a snapshot test in staging that compares old vs new responses.
3. Add a frontend feature flag, defaulting to OFF.
4. Deploy Django.
5. Flip the flag for internal users only. Verify in staging-like conditions.
6. Flip the flag for 5% of real users. Watch error rate, latency.
7. Ramp 5% → 25% → 100% over several days.
8. After ~1 week stable, remove the old Supabase-direct code path and the flag.

Never migrate two endpoints in the same week. Never do a write-endpoint migration before the read-endpoint pattern is proven.

---

## Production safety

- **Never** point dev/local at the production `DATABASE_URL`.
- The **service-role key** belongs ONLY in this backend's env — never in any frontend bundle. Audit `aidoccall.com/src/lib/supabaseClient.js` (lines 27–29) before this work goes live.
- RLS is **on** for `user_roles`. The `core/roles.py` queries run as the connection user; with user-scoped connections, RLS filters automatically. With a service-role connection, RLS is bypassed — your views must filter by `request.user.id` themselves.
- Health check `/api/health/` is the only unauthenticated endpoint. Everything else requires a valid Supabase JWT.

---

## Running in production

```bash
pip install -r requirements.txt
python manage.py collectstatic --noinput
gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 4
```

For the Dr. Murali Adamrit sync button, run one additional worker process:

```bash
python manage.py process_adamrit_sync_jobs
```

Apply `supabase/migrations/20260805_create_adamrit_sync_jobs.sql` before starting
the worker, and configure both Adamrit and DDO service-role credentials from
`.env.example` on the backend only.

Environment for prod:
- `DJANGO_SETTINGS_MODULE=config.settings.prod`
- All values from `.env.example` filled with real prod credentials.
- TLS terminated at the platform level (Render/Fly/Railway/etc.).
