# Starforge Edu — backend

Multi-tenant Django backend for an Uzbek education platform.

## Stack
- Django 6.0 with layered plain-Django APIs; DRF remains only for reports compatibility
- Custom whole-API OpenAPI 3.0 schema, Swagger UI, and Redoc
- django-tenants (schema-per-tenant) with subdomain routing
- Channels + Redis (realtime); Celery + tenant-schemas-celery (background)
- Postgres 16, S3-compatible storage (MinIO in dev / AWS S3 in prod)
- Anthropic Claude (`claude-sonnet-4-6`) with prompt caching
- Eskiz SMS (real client + dev mock)
- uv for dependency management; ruff + mypy + pytest

## Layout
```
config/        Django project: settings split, urls, asgi/wsgi, celery
apps/          One Django app per domain (tenancy, org, users, auth, ...)
core/          Cross-cutting primitives (auth, permissions, HTTP, schema, exceptions)
infrastructure/ External clients (sms, storage, ai, payments, websocket)
celery_tasks/  Background job modules
docker/        Multi-service compose stack
scripts/       create_tenant.py, seed_dev.py, seed_ceo_console.py
docs/          Architecture + ops docs
```

## First run
```bash
cp .env.example .env

# Bring up Postgres + Redis + MinIO
docker compose -f docker/docker-compose.yml up -d postgres redis minio

# Install deps
uv sync --all-groups

# Migrate the public schema (Center + Domain models)
uv run python manage.py migrate_schemas --shared

# Seed a demo tenant: schema=demo, hostname=demo.localhost, +superuser
STARFORGE_ALLOW_LOCAL_DEMO_SEED=1 uv run python scripts/seed_dev.py

# Add the role-native CEO login and representative console data
STARFORGE_ALLOW_LOCAL_DEMO_SEED=1 uv run python scripts/seed_ceo_console.py

# Run the dev server
uv run python manage.py runserver
```

Then hit:
- `http://demo.localhost:8000/admin/`  (login: `admin` / `starforge-dev`)
- `http://demo.localhost:8000/api/schema/swagger-ui/`
- `POST http://localhost:8000/api/v1/auth/login/  {"username":"admin","password":"starforge-platform"}`
- Local CEO console login: username `admin`, password `root`. These intentionally
  weak credentials exist only in the DEBUG-gated `demo` seed and must never be
  copied into staging or production.

### Local CEO console dataset

`scripts/seed_ceo_console.py` is development-only and requires the exact
development settings module, a local/private database target, and the one-shot
`STARFORGE_ALLOW_LOCAL_DEMO_SEED=1` confirmation. It creates a non-admin
`StaffProfile` with the system
`director` account type, so it exercises the same `/auth/role-login/` path as
the CEO console. The Django superusers created by `seed_dev.py` remain separate
and are only for `/admin/` or the platform-admin endpoint.

The script also adds two clearly namespaced campuses, four teachers, two
cohorts, thirteen students, recent lessons, attendance and published progress
results, tuition records, one pending approval, one operational task, one
upcoming leadership meeting, and two CEO notifications. The dataset includes a
cohort-placement case, an enrollment hold, and transparent academic/attendance/
payment signals so the main leadership views are not empty. It intentionally
leaves broad feature-specific samples (content authoring, messaging threads,
payments-provider flows, AI, and report generation) to their own test or
integration workflows. Each demo campus also receives one safe printer, one
token-redacted print connection, and a small completed/queued/failed job history
for the branch Print room.

Every owned record uses a `ceo-demo-*` key or `[CEO demo]` label. Rerunning the
script repairs those reserved records without deleting unrelated data. A rerun
also restores the configured password only if it changed; that password reset
revokes sessions for this demo account by design.

Override the tenant or local password when needed:

```bash
STARFORGE_DEMO_SCHEMA=demo \
STARFORGE_DEMO_CEO_PASSWORD='Another-Local-Password-42!' \
STARFORGE_ALLOW_LOCAL_DEMO_SEED=1 \
uv run python scripts/seed_ceo_console.py
```

With the Docker development stack, use:

```bash
docker compose -f docker/docker-compose.yml run --rm web migrate
docker compose -f docker/docker-compose.yml run --rm \
  -e STARFORGE_ALLOW_LOCAL_DEMO_SEED=1 web python scripts/seed_dev.py
docker compose -f docker/docker-compose.yml run --rm \
  -e STARFORGE_ALLOW_LOCAL_DEMO_SEED=1 web python scripts/seed_ceo_console.py
docker compose -f docker/docker-compose.yml up -d web
```

## Tenancy
- `apps.tenancy.Center` is the tenant model; `apps.tenancy.Domain` maps hostnames.
- `apps.tenancy` is in `SHARED_APPS` only — Center + Domain live in the public schema.
- Everything else is in `TENANT_APPS` — exists once per Center schema.
- All Celery tasks run under the right schema via `tenant-schemas-celery`.
- Channels consumers resolve tenant from hostname before any DB access (`infrastructure/websocket/middleware.py`).

## Auth
- The API uses opaque, revocable server-side sessions. Native clients send the
  returned key as `Authorization: Bearer <access>`. Same-origin browser consoles
  first call `/api/v1/auth/session/`, then request cookie transport at login; the
  key is stored only in a Secure, HttpOnly, SameSite cookie and every unsafe
  cookie-authenticated request requires Django's CSRF header. Production must
  terminate HTTPS on the tenant origin, preserve `Set-Cookie`, proxy `/api` on the
  same origin, and configure its exact trusted origins—the development loopback
  exceptions are not inherited by production.
- Student, teacher, parent, and staff identities and passwords live in their own role tables. Use `POST /api/v1/auth/role-login/ {username, password}` → `{success, data:{access, role, must_change_password}}`. Login is throttled per identifier and per IP.
- `POST /api/v1/auth/login/` exists only on the bare public/platform host. Tenant
  URLConfs do not route it; Django platform admins use it for `/platform/*`, while
  every tenant role account uses `/api/v1/auth/role-login/`.
- **Password reset = OTP** via Eskiz SMS or email: `POST /api/v1/auth/password/reset/{request,confirm}/`. Throttled per-identifier (3/min), per-IP, and globally; responses never reveal whether an account exists.
- Password change revokes all prior sessions and returns one fresh opaque session.
- `/admin/` uses Django's normal session authentication. Role accounts are managed in separate Student, Teacher, Parent, and Staff admin sections; hidden compatibility principals are not selectable in the User table.

## Permissions
- Role-permission matrix lives in `core/permissions.py`.
- Layered views call `check_perm()` for action-level access and query through branch/department-scoped selectors and repositories. Role changes are evaluated live on every request.

## API contract
- Tenant schema: `/api/schema/`; public/platform schema: the same path on the apex host.
- Swagger UI: `/api/schema/swagger-ui/`; Redoc: `/api/schema/redoc/`.
- `uv run python scripts/export_openapi.py --validate` exports `openapi.yaml` and `openapi-public.yaml` and verifies all operation IDs are unique.

## Tests
```bash
uv run pytest
uv run ruff check .
uv run mypy apps core infrastructure config
```

## Documents
- `docs/architecture.md` — tenancy, auth, permissions, events
- `docs/adding-an-app.md` — how to add a new domain app
- `docs/deployment.md` — production deployment notes
