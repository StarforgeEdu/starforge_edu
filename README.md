# Starforge Edu — backend

Multi-tenant Django backend for an Uzbek education platform.

## Stack
- Django 6.0 + DRF 3.17, simplejwt, drf-spectacular
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
core/          Cross-cutting primitives (permissions, viewsets, exceptions)
infrastructure/ External clients (sms, storage, ai, payments, websocket)
celery_tasks/  Background job modules
docker/        Multi-service compose stack
scripts/       create_tenant.py, seed_dev.py
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
uv run python scripts/seed_dev.py

# Run the dev server
uv run python manage.py runserver
```

Then hit:
- `http://demo.localhost:8000/admin/`  (login: `admin` / `starforge-dev`)
- `http://demo.localhost:8000/api/schema/swagger-ui/`
- `POST http://demo.localhost:8000/api/v1/auth/login/  {"username":"admin","password":"starforge-dev"}`

## Tenancy
- `apps.tenancy.Center` is the tenant model; `apps.tenancy.Domain` maps hostnames.
- `apps.tenancy` is in `SHARED_APPS` only — Center + Domain live in the public schema.
- Everything else is in `TENANT_APPS` — exists once per Center schema.
- All Celery tasks run under the right schema via `tenant-schemas-celery`.
- Channels consumers resolve tenant from hostname before any DB access (`infrastructure/websocket/middleware.py`).

## Auth
- JWT everywhere via `djangorestframework-simplejwt` (15-min access, 14-day rotating refresh, blacklist via `token_blacklist`). Tokens are tenant-bound (`schema` claim) and version-bound (`tv` claim) on both the access and refresh paths.
- **Login = username + password**: `POST /api/v1/auth/login/ {username, password}` → `{access, refresh}`. Throttled per-username (5/min) and per-IP (10/min).
- **Password reset = OTP** via Eskiz SMS or email: `POST /api/v1/auth/password/reset/{request,confirm}/`. Throttled per-identifier (3/min), per-IP, and globally; responses never reveal whether an account exists.
- Password change: `POST /api/v1/auth/password/change/` — ends every other session, returns a fresh pair.
- `/admin/` keeps Django sessions; staff may log in with username, phone, or email (`apps.auth.backends.PhoneOrEmailBackend`).

## Permissions
- Role-permission matrix lives in `core/permissions.py`.
- `RolePermission` (action-level) + `ObjectScopedPermission` (branch/department) compose on every `TenantSafeModelViewSet`.

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
