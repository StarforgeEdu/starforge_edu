# DAY 1 — Go live + security spine + people domain

Mission: the repo starts as a never-migrated scaffold — zero migration files, zero tests, placeholder `*Item` models in every domain app, JWT not tenant-bound, permissions fail-open. By EOD: DB migrated and seeded, CI green with coverage ≥ 70% (TD-20), JWT carries `schema`+`tv` claims and is rejected cross-tenant (TD-1), permissions fail-closed and per-action (TD-4/TD-5), platform admin works on the apex (TD-3), `CenterSettings` live (TD-13), Student/Parent/Guardian/Teacher/Cohort real, org structure complete, the tenant-isolation test green.

Merge order today (ROADMAP §2.3): **A → C → B → F → D → E**. Lane A owns the migration graph and merges FIRST; every other lane deletes its locally generated migrations and regenerates them after rebasing on master (`uv run python manage.py makemigrations <your apps>`). Numbering conflicts: later merger renumbers or uses `makemigrations --merge`.

## Prerequisites

There is no yesterday. Verify the starting state before branching:

- [ ] `git log --oneline` shows the single bootstrap commit (`9f091b4`) or later; `agents/WORKLOG.md` read end-to-end (other Day-1 lanes may already have posted interface announcements — check before you start, and re-check before you merge).
- [ ] `docker compose -f docker/docker-compose.yml up -d postgres redis minio` then `docker compose -f docker/docker-compose.yml exec postgres pg_isready -U starforge` succeeds.
- [ ] `uv sync --all-groups` succeeds; `uv run python manage.py check` passes (it will, pre-migrations).
- [ ] Confirm zero migrations exist: `Glob apps/**/migrations/0*.py` returns nothing. If Lane A has already merged, pull master and skip your own bootstrap assumptions.

---

## Lane A — Bootstrap, CI, ops basics, TD-17 bug fixes

**Objective:** make the project actually run: generate the entire initial migration graph, migrate, seed, smoke the OTP flow; wire coverage into CI; add health/request-ID/JSON-logging ops basics; fix every TD-17 audit bug. Implements TASKS §0, §1; TD-16, TD-17, TD-20.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D1-LA-1 | Bootstrap per TASKS §0: compose up, `uv run python manage.py makemigrations` for ALL apps, inspect `users`/`tenancy`/`org` migrations, commit as `chore(migrations): initial migration graph for v1 apps`, `migrate_schemas --shared`, run `scripts/seed_dev.py` | `apps/*/migrations/0001_initial.py` (19 apps) | Fresh DB: `migrate_schemas --shared` exits 0; seed prints created Center `demo` + superuser; `http://demo.localhost:8000/admin/` login works; swagger-ui renders | — |
| D1-LA-2 | Smoke OTP flow end-to-end (TASKS §0 last 4 items) and record exact curl commands in WORKLOG | none (verification) | `POST /api/v1/auth/otp/request/` → mock code on stdout; `verify` → `{access, refresh}`; `GET /api/v1/users/me/` → 200 | D1-LA-1 |
| D1-LA-3 | TD-17: Eskiz fixes — 401-retry recursion guard (single re-auth retry, then raise) + hardcoded sender `"4546"` → `ESKIZ_FROM` env setting | `infrastructure/sms/eskiz_client.py`, `config/settings/base.py` (add `ESKIZ_FROM=(str, "4546")` to `env` + export) | Unit test: client that gets 401 twice raises after exactly one re-auth, no recursion; sender read from `settings.ESKIZ_FROM` [OWNER:O-1 for real creds — mock path unaffected] | — |
| D1-LA-4 | TD-17: Anthropic Redis cache key must include `max_tokens` + `effort` | `infrastructure/ai/anthropic_client.py` | Two calls identical except `max_tokens` produce different cache keys (unit test on key builder) [OWNER:O-2 not needed — key builder is local] | — |
| D1-LA-5 | TD-17: wire `docker/entrypoint.sh` (decision: KEEP and wire, do not delete) — set `ENTRYPOINT ["/entrypoint.sh"]` in `docker/Dockerfile`, reference commands from `docker/docker-compose.yml`, fix `migrate` case to run `python manage.py migrate_schemas` (shared **and** tenant) | `docker/entrypoint.sh`, `docker/Dockerfile`, `docker/docker-compose.yml` | `docker compose run web migrate` migrates public AND tenant schemas; decision + rationale documented in WORKLOG | D1-LA-1 |
| D1-LA-6 | TD-17: align README AI model name — README says `claude-opus-4-7`, `config/settings/base.py:367` says `claude-sonnet-4-6`. Align README to the settings value | `README.md` | `grep claude README.md` shows only `claude-sonnet-4-6` | — |
| D1-LA-7 | TD-17: kill OTP auto-registration — `verify_otp` in `apps/auth/services.py` currently `get_or_create`s users. Change to: look up existing user; if none and registration closed, raise `ValidationException(code="user_not_found")`. Gate = `_registration_open()` helper reading new setting `OPEN_REGISTRATION_DEFAULT = False`; Lane B rewires the helper to `CenterSettings.open_registration` (announce the helper signature in WORKLOG) | `apps/auth/services.py`, `config/settings/base.py` | Verify with unknown identifier → 400 `user_not_found`; with seeded superuser phone → token pair; helper name/location published in WORKLOG | — |
| D1-LA-8 | Health endpoints: `/healthz/live` (200 always) + `/healthz/ready` (DB `SELECT 1` + Redis ping, 503 with `{"error":{"code":"not_ready"}}` on failure). Implement as `HealthCheckMiddleware` in **new** `core/middleware.py` placed BEFORE `TenantMainMiddleware` so probes work on ANY Host header without tenant resolution; no auth, no throttle | `core/middleware.py` (new), `config/settings/base.py` MIDDLEWARE | `curl -H "Host: anything" localhost:8000/healthz/live` → 200 on both apex and tenant hosts; ready → 503 when Redis stopped | — |
| D1-LA-9 | `RequestIDMiddleware` in `core/middleware.py`: accept inbound `X-Request-ID` or generate uuid4, store in a contextvar, echo response header; `RequestIDFilter` in `core/logging_filters.py` injects it into log records (extend the `verbose` formatter) | `core/middleware.py`, `core/logging_filters.py`, `config/settings/base.py` LOGGING | Response always carries `X-Request-ID`; inbound value is echoed verbatim; log lines contain it (test with `caplog`) | D1-LA-8 |
| D1-LA-10 | Structured JSON logging in prod settings only (decision: hand-rolled `JsonFormatter` in `core/logging_filters.py` — no new dep, stays within TD-16) | `core/logging_filters.py`, `config/settings/production.py` | With production settings, a log line parses as JSON with keys `ts, level, logger, msg, schema, request_id`; dev/test keep human format | D1-LA-9 |
| D1-LA-11 | CI coverage gate (TD-20): add `pytest-cov` to `pyproject.toml` (TD-16), test job runs `uv run pytest -q --cov=apps --cov=core --cov-fail-under=70` | `.github/workflows/ci.yml`, `pyproject.toml` | CI test job fails when coverage < 70; green on master at EOD | D1-LA-1 |
| D1-LA-12 | `Makefile` with `up`, `migrate`, `seed`, `test`, `lint`, `schema`, `makemigrations` targets (each delegating to `uv run ...`); `.github/dependabot.yml` weekly for `pip` + `github-actions` | `Makefile` (new), `.github/dependabot.yml` (new) | `make test` runs pytest; dependabot file passes `gh api` schema validation (or visual check against GitHub docs) | — |
| D1-LA-13 | Sentry config-only: `SENTRY_DSN` env var, init in `config/settings/production.py` only when set [OWNER:O-10]. Prometheus + schema-diff CI: explicitly DEFERRED — note in WORKLOG (schema-diff lands D5-D, metrics D5-A) | `config/settings/production.py`, `pyproject.toml` | App boots with and without `SENTRY_DSN`; no DSN committed | — |

**Models:** none (Lane A creates no domain models — it generates migrations for everyone else's existing scaffold).

**Endpoints:** `GET /healthz/live` and `GET /healthz/ready` — unauthenticated, both URLConfs bypassed via middleware (no permission code).

**Signals/Celery:** none. Verify `celery_tasks/cleanup_tasks.py::purge_expired_otps` imports clean; beat registration is Lane B's D1-LB-6.

**Tests required** (agents/TESTING.md unit tier): `apps/auth/tests/test_otp_registration_gate.py` (closed-registration 400, existing-user OK), `infrastructure` tests for Eskiz retry guard + Anthropic cache key, `core` tests for request-ID echo and healthz live/ready (ready failure path with mocked broken Redis).

**Publish to WORKLOG:** (1) migration graph committed — all lanes must rebase + regenerate; (2) `_registration_open()` helper location/signature for Lane B; (3) `core/middleware.py` exists — Lane B appends its middleware there, do not recreate; (4) exact smoke-flow curl commands; (5) entrypoint decision.

---

## Lane C — Auth/JWT hardening (merges second)

**Objective:** make the token the tenant boundary: `schema` + `tv` claims enforced in `core/authentication.py` (TD-1), fail-closed (TD-4) per-action (TD-5) permissions across all 18 existing viewsets, refresh-reuse detection, devices, OTP abuse controls. Implements TASKS §3; TD-1, TD-4, TD-5.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D1-LC-1 | Add to `apps/users/models.py` `User`: `token_version` (PositiveIntegerField, default=1), `birthdate` (DateField, null/blank), `gender` (CharField(8), choices m/f, blank), `preferred_language` (CharField(8), choices uz/en/ru, default "uz"). Migration `users/0002` after rebase on A | `apps/users/models.py`, `apps/users/serializers.py` | Fields appear in `/api/v1/users/me/` response; migration applies on fresh DB | A merged |
| D1-LC-2 | TD-1: `issue_token_pair` in `apps/auth/services.py` adds claims `schema` (= `connection.schema_name`), `tv` (= `user.token_version`), `roles` (list from active RoleMemberships) to BOTH access and refresh | `apps/auth/services.py` | Decoded access token contains all three claims; refresh carries `schema`+`tv` | D1-LC-1 |
| D1-LC-3 | TD-1: new `core/authentication.py` → `TenantAwareJWTAuthentication(JWTAuthentication)`: reject `schema` ≠ `connection.schema_name` with 401 `{"error":{"code":"tenant_mismatch"}}`; reject `tv` ≠ `user.token_version` with 401 `code="token_stale"`. Swap into `REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]` replacing plain `JWTAuthentication` | `core/authentication.py` (new), `config/settings/base.py` | Lane E's isolation test goes green: token minted on tenant_a → 401 `tenant_mismatch` on tenant_b host; bumping `token_version` 401s old tokens | D1-LC-2 |
| D1-LC-4 | TD-4: `RolePermission.has_permission` in `core/permissions.py` returns **False** when the view declares no mapping (currently `return True` at line 126) | `core/permissions.py` | A viewset with no `required_perms` → 403 for any non-superuser; test proves it | — |
| D1-LC-5 | TD-5: replace flat `required_perm` with `required_perms: dict[action, code]`. Add `core/permissions.py::default_perms(resource)` → `{list/retrieve: "<r>:read", create/update/partial_update/destroy: "<r>:write"}` (custom `@action`s must be mapped explicitly). Migrate ALL 18 existing viewsets (`apps/{org,students,parents,teachers,cohorts,schedule,attendance,academics,assignments,content,printing,finance,payments,notifications,ai,audit,reports}/views.py`, `apps/users/views.py` DeviceViewSet); delete `required_perm` support entirely | `core/permissions.py`, 18× `apps/*/views.py` | `grep -rn "required_perm " apps core` → 0 hits; `required_perms` on every viewset; non-director role gets 403 on `destroy` where matrix grants only `:read` | D1-LC-4 |
| D1-LC-6 | Refresh reuse detection: custom refresh serializer/view in `apps/auth/views.py` — if presented refresh is already blacklisted, blacklist ALL `OutstandingToken`s for that user, bump `token_version`, return 401 `code="refresh_reused"` | `apps/auth/views.py`, `apps/auth/services.py` | Test: rotate once, replay old refresh → 401 `refresh_reused` AND the new refresh is now dead too | D1-LC-2 |
| D1-LC-7 | `token_version` bump on password change + role change: `apps/users/receivers.py` (new, wired in `apps/users/apps.py.ready()`) — `post_save`/`post_delete` on `RoleMembership` and a `set_password` service hook bump `token_version` via `F()+1` | `apps/users/receivers.py` (new), `apps/users/apps.py`, `apps/users/services.py` | Granting/revoking a RoleMembership invalidates live access tokens (401 `token_stale` on next request) | D1-LC-3 |
| D1-LC-8 | Logout-everywhere: `POST /api/v1/auth/logout-all/` — blacklist all user's outstanding refreshes + bump `token_version` | `apps/auth/views.py`, `apps/auth/urls.py` | After call, both refresh AND access (old `tv`) are rejected; returns 204 | D1-LC-7 |
| D1-LC-9 | Devices: auto-register/update `Device` on OTP verify when client sends `device_id` + `platform` (UA captured server-side); `POST /api/v1/users/devices/` registers/updates `push_token` [OWNER:O-7 — store only, no push send today]; `DELETE /api/v1/users/devices/{id}/` sets `revoked_at` (soft) | `apps/auth/views.py`, `apps/auth/serializers.py`, `apps/users/views.py`, `apps/users/serializers.py` | Verify-with-device creates Device row; list shows it; delete sets `revoked_at` not row deletion; revoked devices excluded from list | D1-LC-1 |
| D1-LC-10 | OTP cooldown + enumeration cap: in `send_otp` — (a) last OTP for identifier < `OTP_COOLDOWN_SECONDS` (default 60, read from CenterSettings via Lane B's accessor once merged — leave a settings fallback) → `ThrottledException`; (b) cache-based per-IP cap: > 5 distinct identifiers per IP per hour → 429 | `apps/auth/services.py`, `apps/auth/views.py` (pass IP), `config/settings/base.py` | 2nd request within 60s → 429 `throttled`; 6th distinct identifier from one IP in an hour → 429; both covered by tests | — |
| D1-LC-11 | OTP audit events (pre-§19): define signals `otp_requested`, `otp_verified`, `otp_failed` in `apps/auth/signals.py` (new), fire with `identifier, ip, user_agent`; today's consumer = structured log line on `starforge.auth`; Day 3 Lane D attaches AuditLog | `apps/auth/signals.py` (new), `apps/auth/services.py`, `apps/auth/views.py` | Signals fire on request/verify/wrong-code (assert via test receiver); log lines include ip + request_id | D1-LC-10 |
| D1-LC-12 | `last_seen_at` touch: in `TenantAwareJWTAuthentication.authenticate`, update `User.last_seen_at` (single `update()` query) only when stale > 60s | `core/authentication.py` | Two requests 1s apart → one UPDATE; value advances after fixture clock shift | D1-LC-3 |
| D1-LC-13 | Permission caching per request: `_user_roles` → `get_user_roles(request)` memoized on `request._role_cache`; `RolePermission` + `ObjectScopedPermission` use it | `core/permissions.py` | Query-count test: list endpoint does exactly 1 RoleMembership query regardless of permission checks count | D1-LC-5 |

**Models:** only the `User` field additions above (D1-LC-1). No new models.

**Endpoints:** `POST /api/v1/auth/logout-all/` (auth required, no role code — any authenticated user, returns 204); `POST /api/v1/users/devices/` + `GET /api/v1/users/devices/` + `DELETE /api/v1/users/devices/{id}/` (`required_perms` self-scoped: queryset filtered to `request.user`, no matrix code needed beyond IsAuthenticated — document this exception in the view docstring).

**Signals/Celery:** `otp_requested/verified/failed` signals (idempotent — log-only consumers today). Receivers for `RoleMembership` token bump are idempotent by construction (`F()+1` is fine even on double-fire; a stale token is the goal).

**Tests required** (TESTING.md auth tier): `test_tenant_mismatch_401` (coordinate with Lane E — they wrote it first, you make it green; announce in WORKLOG the moment TD-1 merges), `test_token_stale_after_role_change`, `test_refresh_reuse_revokes_all` (TASKS §26), `test_logout_all`, `test_otp_cooldown_429`, `test_otp_ip_distinct_identifier_cap`, `test_fail_closed_no_mapping_403`, `test_per_action_perms_teacher_cannot_destroy_students`, query-count test for role caching.

**Publish to WORKLOG:** (1) TD-1 merged — Lane E un-skips the isolation test; (2) `required_perms` contract + `default_perms()` helper — Lanes D/F MUST use it on every new viewset; (3) token claims shape `{schema, tv, roles}`; (4) signal names in `apps/auth/signals.py` for Day-3 audit.

---

## Lane B — Tenancy lifecycle + TD-3 + CenterSettings (merges third)

**Objective:** complete the Center lifecycle (reserved slugs, dedup, deactivation→503, trial expiry, archival), fix the broken apex admin via TD-3 public-schema users, and ship `CenterSettings` (TD-13) — the per-school knob store every later lane reads. Implements TASKS §2; TD-3, TD-13, TD-17 (open_registration wiring).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D1-LB-1 | TD-3: add `apps.users.apps.UsersConfig`, `apps.auth.apps.AuthAppConfig`, `rest_framework_simplejwt.token_blacklist` to `SHARED_APPS` in `config/settings/base.py` (keep in `TENANT_APPS`); update `scripts/seed_dev.py` to also create a **public-schema** platform superuser; write `docs/adr/ADR-007-public-schema-users.md` | `config/settings/base.py`, `scripts/seed_dev.py`, `docs/adr/ADR-007-public-schema-users.md` (new) | `migrate_schemas --shared` creates `public.users_user`; apex `http://localhost:8000/admin/` login works with platform superuser; tenant users unaffected | A merged |
| D1-LB-2 | TD-13: `CenterSettings` model in `apps/org/models.py` (decision: org app — it is per-center configuration; coordinate with Lane F who also edits this file — APPEND only, announce in WORKLOG, F merges after you and resolves migration numbering) + `CenterSettings.load()` classmethod (`get_or_create(pk=1)`); auto-create inside `provision_center` via `schema_context` | `apps/org/models.py`, `apps/tenancy/services.py` | One row per tenant schema; `load()` always returns the singleton; provisioning a center creates it | A merged |
| D1-LB-3 | Settings endpoint: `GET/PATCH /api/v1/org/settings/` (singleton APIView, `required_perms={"retrieve":"org:read","partial_update":"org:write"}` equivalent for APIView via explicit check) | `apps/org/views.py`, `apps/org/serializers.py`, `apps/org/urls.py` | Director PATCHes `late_threshold_minutes` → persisted; teacher GET 200, PATCH 403 | D1-LB-2, C merged |
| D1-LB-4 | Wire TD-17 `open_registration`: replace Lane A's `_registration_open()` settings fallback with `CenterSettings.load().open_registration` | `apps/auth/services.py` | With flag on: unknown identifier auto-creates user on verify; flag off (default): 400 `user_not_found` | D1-LB-2 |
| D1-LB-5 | `provision_center` hardening in `apps/tenancy/services.py`: validate slug `^[a-z][a-z0-9_]{0,62}$`; reject `RESERVED_SLUGS = {"public","admin","www","api","static","media"}` → `ValidationException(code="slug_reserved")`; duplicate slug → `ValidationException(code="slug_taken")` (pre-check, not IntegrityError); Center delete service refuses when tenant has > 0 users unless `force=True` | `apps/tenancy/services.py`, `scripts/create_tenant.py` | Each rejection path unit-tested with its error code; `create_tenant.py` surfaces clean errors | — |
| D1-LB-6 | Deactivation → 503: `InactiveTenantMiddleware` appended to `core/middleware.py` (after `TenantMainMiddleware`): tenant resolved AND `is_active=False` → `503 {"error":{"code":"center_inactive"}}` (healthz already bypassed by Lane A's middleware ordering) | `core/middleware.py`, `config/settings/base.py` MIDDLEWARE | Set demo `is_active=False` → all tenant API routes 503 with envelope; public schema unaffected | A merged |
| D1-LB-7 | Trial expiry beat task: `celery_tasks/tenancy_tasks.py::deactivate_expired_trials` (public-schema task: `Center.objects.filter(is_active=True, on_trial=True, trial_ends_at__lt=now()).update(is_active=False)` — idempotent by filter); register hourly + `purge_expired_otps` daily via `CELERY_BEAT_SCHEDULE` in `config/settings/base.py` (DatabaseScheduler ingests it; D4-F consolidates) | `celery_tasks/tenancy_tasks.py` (new), `config/settings/base.py` | Task run twice flips exactly the expired centers once; eager test asserts state change; beat entries visible in django-celery-beat admin | D1-LB-6 |
| D1-LB-8 | Archival soft-delete: `archive_center(center)` service — `ALTER SCHEMA <slug> RENAME TO _archived_<slug>_<YYYYMMDD>`, update `schema_name`, set `is_active=False`, add `archived_at` (DateTimeField, null) to `Center`; management command `python manage.py archive_center <slug>` | `apps/tenancy/models.py`, `apps/tenancy/services.py`, `apps/tenancy/management/commands/archive_center.py` (new) | After archive: hostname 503s, schema renamed (assert via `information_schema`), Center row retained with `archived_at` set | D1-LB-5 |
| D1-LB-9 | Domain primary management: extend `apps/tenancy` platform API — list/add domains for a center, `POST /api/v1/platform/centers/{id}/domains/{domain_id}/set-primary/` (exactly one `is_primary=True` per center, enforced in service). TXT-record ownership verification: stub `verify_domain_txt()` returning mock-pass [OWNER:O-8]; wildcard DNS noted as O-8, no code | `apps/tenancy/views.py`, `apps/tenancy/serializers.py`, `apps/tenancy/urls.py`, `apps/tenancy/services.py` | set-primary flips flags atomically (old primary demoted in same transaction); platform-staff-only (`IsAdminUser` — now functional thanks to TD-3) | D1-LB-1 |

**Models** — `CenterSettings` (apps/org, tenant schema, singleton pk=1):

| Field | Type / constraint |
|---|---|
| open_registration | BooleanField, default=False (TD-17) |
| grading_scheme | CharField(16), choices letter/gpa/percentage, default "percentage" |
| late_threshold_minutes | PositiveSmallIntegerField, default=10 |
| attendance_correction_window_hours | PositiveSmallIntegerField, default=24 |
| assignment_grace_minutes | PositiveSmallIntegerField, default=0 |
| max_upload_mb | PositiveIntegerField, default=200 |
| allowed_file_types | JSONField, default=list (pdf, mp4, pptx, docx, mp3, jpg, png) |
| currency_primary / currency_secondary | CharField(3), defaults "UZS" / "USD" |
| fx_source | CharField(32), default "cbu" |
| quiet_hours_start / quiet_hours_end | TimeField, defaults 22:00 / 07:00 |
| otp_channel_prefs | JSONField, default `{"sms": true, "email": true}` |
| otp_cooldown_seconds | PositiveSmallIntegerField, default=60 |
| student_id_pattern | CharField(64), default `"{CODE}-{YYYY}-{NNNNN}"` |
| center_code | CharField(16), blank (empty → uppercase tenant slug) |

Plus `Center.archived_at` (DateTimeField, null/blank) in apps/tenancy.

**Endpoints:** `GET/PATCH /api/v1/org/settings/` (org:read / org:write); platform: `GET/POST /api/v1/platform/centers/{id}/domains/`, `POST .../domains/{domain_id}/set-primary/` (IsAdminUser, public URLConf).

**Signals/Celery:** `deactivate_expired_trials` (hourly, idempotent filter-update), `purge_expired_otps` (daily, already real in `celery_tasks/cleanup_tasks.py`).

**Tests required** (TESTING.md tenancy tier): `test_reserved_slug_rejected`, `test_duplicate_slug_clean_error`, `test_inactive_center_503`, `test_trial_expiry_flips_is_active` (idempotency: run twice), `test_archive_renames_schema`, `test_public_schema_platform_admin_login`, `test_center_settings_singleton_and_patch_perms`, `test_set_primary_domain_atomic`.

**Publish to WORKLOG:** (1) `CenterSettings.load()` accessor + full field list — Lanes C (otp_cooldown_seconds), D (student_id_pattern, center_code), and all Day-2+ lanes consume it; (2) TD-3 landed: SHARED_APPS changed — everyone re-runs `migrate_schemas`; (3) ADR-007 path; (4) beat-schedule registration mechanism for later task authors.

---

## Lane F — Org completion (merges fourth)

**Objective:** finish the physical/organizational layer: rooms, working hours, holidays, department heads, capacity caps, transfer history, soft-delete rules. Implements TASKS §4; TD-5 (new viewsets use `required_perms`), TD-13 (no magic numbers — caps live on rows).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D1-LF-1 | `Room` model + CRUD viewset (`required_perms` via `default_perms("org")`, `object_scope="branch"`), filter by `branch`, search by `name` | `apps/org/models.py`, `serializers.py`, `views.py`, `urls.py`, `selectors.py` | CRUD works for director; teacher 403 on write; rooms filterable by branch; unique (branch, name) enforced → 400 envelope | B merged (shared models.py) |
| D1-LF-2 | `BranchWorkingHours` — one row per (branch, weekday); exposed as nested read on Branch serializer + bulk-set endpoint `PUT /api/v1/org/branches/{id}/working-hours/` (replaces all 7 rows transactionally) | `apps/org/models.py`, `views.py`, `serializers.py`, `services.py` | PUT with 7 rows replaces atomically; `opens_at >= closes_at` (non-closed day) → 400; GET branch embeds hours | D1-LF-1 |
| D1-LF-3 | `BranchHoliday` — per-branch dates layered over national holidays (national seeding is D2-A §9; today only the branch table) + CRUD nested route `GET/POST/DELETE /api/v1/org/branches/{id}/holidays/` | `apps/org/models.py`, `views.py`, `urls.py` | unique (branch, date); list ordered by date; only director/IT write (matrix) | D1-LF-1 |
| D1-LF-4 | Department head + budget: `Department.head` = FK to `users.User` (SET_NULL, null/blank) — decision: FK targets User, NOT TeacherProfile, because Lane D merges after you; service `set_department_head()` validates `TeacherProfile` exists for that user via `django.apps.apps.get_model("teachers","TeacherProfile")` wrapped in try/except `LookupError` (validation no-ops until D lands; goes strict automatically after). `Department.budget` = DecimalField(14,2, null) | `apps/org/models.py`, `services.py`, `serializers.py` | Pre-D merge: head assignable; post-D: assigning a non-teacher raises `ValidationException(code="head_not_teacher")` (Lane D adds the cross-test D1-LD-10) | B merged |
| D1-LF-5 | Capacity soft caps: `Branch.max_students`, `Branch.max_teachers` (PositiveIntegerField, null = unlimited); serializer exposes `capacity_status` (`{current_students, max_students, over}`) — counts via late `apps.get_model` lookups, `None`-safe before Lane D merges | `apps/org/models.py`, `serializers.py`, `selectors.py` | Caps stored/PATCHable; `capacity_status` present in Branch detail; soft = never blocks writes (assert enrollment still succeeds over cap — Lane D test) | D1-LF-1 |
| D1-LF-6 | `BranchTransfer` history model — decision: FK to `users.User` (the student's user), NOT `students.StudentProfile`, to avoid a migration dependency on Lane D which merges later. `record_transfer()` service stub; full cascade (attendance/schedule/finance) is D2+ — document the stub boundary | `apps/org/models.py`, `services.py` | Model migrates; `record_transfer(user, from_branch, to_branch, reason, actor)` creates a row; list endpoint read-only `audit-style` ordering `-created_at` | D1-LF-1 |
| D1-LF-7 | Branch soft-delete rules: add `Branch.archived_at` (DateTimeField, null); `destroy` action overridden → archives (sets `archived_at`, `is_active=False`) instead of deleting; refuse archival when branch has active students (late `apps.get_model` lookup, no-op pre-D) → 409 `ValidationException(code="branch_has_active_students")` | `apps/org/models.py`, `views.py`, `services.py` | DELETE returns 204 but row persists archived; archived branches excluded from default list (filter `archived_at__isnull=True`); refusal path tested after D merges | D1-LF-1 |
| D1-LF-8 | Matrix entries (TD-5, additive append to `ROLE_PERMISSION_MATRIX` in `core/permissions.py`): `Role.IT` += `"org:*"`; confirm Branch/Department/Room writes restricted to director/IT per TASKS §4 | `core/permissions.py` | Teacher: org GET 200 / POST 403; IT: POST 201 | C merged |

**Models** (all apps/org; every FK indexed, `__str__` + `Meta.ordering` on each):

- `Room`: branch FK(Branch, CASCADE, related_name="rooms") · name CharField(100) · capacity PositiveSmallIntegerField(default=0) · equipment JSONField(default=list) · is_active Boolean(default=True) · notes TextField(blank) · created_at/updated_at. `unique_together (branch, name)`.
- `BranchWorkingHours`: branch FK(related_name="working_hours") · weekday PositiveSmallIntegerField(choices 0–6) · opens_at TimeField · closes_at TimeField · is_closed Boolean(default=False). `unique_together (branch, weekday)`; CheckConstraint `opens_at < closes_at OR is_closed`.
- `BranchHoliday`: branch FK(related_name="holidays") · date DateField · name CharField(200) · is_working_day_override Boolean(default=False). `unique_together (branch, date)`, ordering `("date",)`.
- `BranchTransfer`: user FK(users.User, CASCADE, related_name="branch_transfers") · from_branch FK(Branch, PROTECT, related_name="transfers_out") · to_branch FK(Branch, PROTECT, related_name="transfers_in") · reason CharField(64, blank) · actor FK(users.User, SET_NULL, null, related_name="transfers_made") · created_at. Ordering `("-created_at",)`.
- `Department` additions: head FK(users.User, SET_NULL, null/blank, related_name="headed_departments") · budget DecimalField(14,2, null/blank).
- `Branch` additions: max_students/max_teachers PositiveIntegerField(null/blank) · archived_at DateTimeField(null/blank).

**Endpoints:** `/api/v1/org/rooms/` CRUD (org:read/org:write, object_scope=branch) · `PUT /api/v1/org/branches/{id}/working-hours/` (org:write) · `GET/POST/DELETE /api/v1/org/branches/{id}/holidays/` (org:read/write) · `GET /api/v1/org/transfers/` read-only (org:read).

**Signals/Celery:** none today.

**Tests required:** `test_room_crud_and_branch_scope`, `test_working_hours_bulk_replace_atomic`, `test_working_hours_invalid_range_400`, `test_holiday_unique_per_branch_date`, `test_department_head_late_validation` (skip-marked until D merges, un-skip same day), `test_branch_archive_instead_of_delete`, `test_branch_archived_excluded_from_list`, query-count test on Branch list with nested hours (`select_related`/`prefetch_related` per TESTING.md).

**Publish to WORKLOG:** (1) `Room` available — Lane D's `Cohort.default_room` FK target; (2) `Department.head` is a **User** FK + `set_department_head()` validation contract — Lane D must ensure TeacherProfile exists before assigning heads; (3) `record_transfer()` signature for Day-2 attendance/schedule cascades; (4) org migration numbering after merge (`org/000X`).

---

## Lane D — People domain (merges fifth)

**Objective:** replace the placeholders in students/parents/teachers/cohorts with the real people domain: profiles, Guardian links, enrollment state machine, generated student IDs, CSV import, search, cohorts with memberships and co-teachers. Implements TASKS §5, §6, §7, §8; TD-5, TD-11 (EncryptedTextField), TD-13 (pattern from CenterSettings), TD-16 (`pillow`, `cryptography`).

**DELETE the placeholder models first:** remove `StudentItem` (`apps/students/models.py`), `ParentItem` (`apps/parents/models.py`), `TeacherItem` (`apps/teachers/models.py`), `CohortItem` (`apps/cohorts/models.py`) plus their serializers/viewsets/router entries. Regenerate each app's migrations from scratch after rebasing on F (you merge after F; your `0001_initial` per app replaces the scaffold one Lane A committed — delete and regenerate, then `makemigrations --merge` if needed).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D1-LD-1 | TD-11 foundation: `core/fields.py` (new) — `EncryptedTextField`/`EncryptedCharField` (Fernet from `cryptography`); `FIELD_ENCRYPTION_KEY` env setting, dev/test default key in `config/settings/{development,test}.py`, prod REQUIRES env [OWNER:O-11] | `core/fields.py` (new), `config/settings/base.py`, `pyproject.toml` | Round-trip test passes; raw `SELECT medical_notes` returns ciphertext not plaintext; prod settings raise ImproperlyConfigured without key | — |
| D1-LD-2 | `StudentProfile` + `EnrollmentEvent` + `StudentIdCounter` models, services (`create_student`, `transition_enrollment`), selectors, read/write serializers, viewset (`default_perms("students")`, `object_scope="branch"`, django-filter on status/branch/cohort, ordering) | `apps/students/{models,services,selectors,serializers,views,urls}.py` | CRUD green for registrar/director; teacher read-only; student model matches field table below; list filtered by `?status=active&branch=` | F merged, D1-LD-1 |
| D1-LD-3 | Enrollment state machine: allowed transitions `lead→application→accepted→enrolled→active→{graduated, withdrawn}` (+ `withdrawn→application` re-enroll); every transition writes an `EnrollmentEvent` with `reason_code`; illegal transition → 400 `code="invalid_transition"`. Endpoint `POST /api/v1/students/{id}/transition/ {"to_status","reason_code","note"}` | `apps/students/services.py`, `views.py` | Parameterized test over the full transition table (legal + illegal); event rows created; `enrollment_date` auto-set on first `enrolled` | D1-LD-2 |
| D1-LD-4 | Student ID generator: `generate_student_id()` in `apps/students/services.py` — pattern from `CenterSettings.student_id_pattern` (`{CODE}` = center_code or uppercase slug, `{YYYY}` = year, `{NNNNN}` = zero-padded seq from `StudentIdCounter` row locked with `select_for_update`); assigned on `create_student` | `apps/students/services.py`, `models.py` | Demo tenant: first student gets `DEMO-2026-00001`, 42nd gets `DEMO-2026-00042`; concurrent-create test produces no duplicates; pattern change in settings respected | D1-LD-2, B merged |
| D1-LD-5 | Bulk CSV import (decision: stdlib `csv`, NOT pandas — not in TD-16; logged in WORKLOG): `POST /api/v1/students/import/` multipart; per-row create user+profile inside one transaction; response `{created, errors: [{row, detail}]}`; row errors don't abort valid rows (savepoints) | `apps/students/services.py`, `views.py` | 10-row fixture with 2 bad rows → 8 created, 2 row-errors reported; idempotent re-run skips existing phones | D1-LD-4 |
| D1-LD-6 | Search + birthday: DRF SearchFilter over `user__first_name, user__last_name, user__phone, student_id` (decision: icontains search today; Postgres FTS deferred to D5-C perf pass); `GET /api/v1/students/birthdays/?days=7&branch=&cohort=` using `User.birthdate` (Lane C's field) | `apps/students/selectors.py`, `views.py` | `?search=demo` matches by partial name/phone/ID; birthdays endpoint returns students with birthdate in window, filterable | D1-LD-2, C merged |
| D1-LD-7 | `ParentProfile` + `Guardian` + `PickupAuthorization` models, viewsets (`default_perms("parents")`); Guardian is THE sanctioned parents→students link (string FK `"students.StudentProfile"` — documented exception to the no-cross-role-FK rule, per docs/adding-an-app.md routing note); enforce one primary guardian per student via conditional UniqueConstraint | `apps/parents/{models,services,selectors,serializers,views,urls}.py` | Linking 2nd primary guardian → 400; `GET /api/v1/parents/{id}/students/` lists linked students (siblings case); parent role queryset scoped to own children in selectors (TD-5 `read_own_children`) | D1-LD-2 |
| D1-LD-8 | `TeacherProfile` model + viewset (`default_perms("teachers")`, `object_scope="branch"`); fields per table; substitute pool = `is_substitute` flag + `?is_substitute=true` filter; availability calendar + load report deferred to D2 (announce) | `apps/teachers/{models,services,selectors,serializers,views,urls}.py` | CRUD green; department FK validates department belongs to same branch (service check) | F merged |
| D1-LD-9 | `Cohort` + `CohortMembership` + `CohortTeacher` (co-teachers) models, services (`enroll_student_in_cohort`, `move_student`), viewset (`default_perms("cohorts")`, `object_scope="branch"`); mid-term move = close old membership (`end_date=today`, `moved_reason`) + create new one in a single transaction + fire `audit hook stub` (plain signal `cohort_member_moved` in `apps/cohorts/signals.py`; Day-3 audit consumes); archive flag honored: archived cohorts read-only (writes → 400 `code="cohort_archived"`) | `apps/cohorts/{models,services,selectors,serializers,views,urls}.py`, `apps/cohorts/signals.py` (new) | One ACTIVE membership per (student) per cohort enforced by conditional UniqueConstraint; move endpoint `POST /api/v1/cohorts/{id}/move-student/` leaves history intact (old row end-dated, not deleted); capacity exceeded → soft warning field in response, never a block (Lane F contract) | D1-LD-2, D1-LD-8 |
| D1-LD-10 | Matrix + cross-lane glue: append `ROLE_PERMISSION_MATRIX` entries — `Role.REGISTRAR` += `"parents:*", "teachers:read"`; un-skip Lane F's `test_department_head_late_validation` and add `test_set_department_head_requires_teacher_profile`; update `scripts/seed_dev.py` to seed 1 branch, 1 department, 2 teachers, 1 cohort, 5 students, 2 parents with guardian links | `core/permissions.py`, `scripts/seed_dev.py`, `apps/org/tests/` | Seed is idempotent (re-run = no dupes); head validation now strict | D1-LD-8, F merged |

**Models** (key fields; all FKs `db_index` by default, add `db_index=True` to every status/date field filtered in selectors):

- `StudentProfile` (apps/students): user OneToOne(users.User, CASCADE, related_name="student_profile") · student_id CharField(32, unique, db_index) · status CharField(16, choices lead/application/accepted/enrolled/active/graduated/withdrawn, default "lead", db_index) · branch FK(org.Branch, PROTECT, related_name="students") · current_cohort FK("cohorts.Cohort", SET_NULL, null/blank, related_name="current_students") · enrollment_date DateField(null/blank) · academic_level CharField(64, blank) · medical_notes **EncryptedTextField**(blank) · emergency_contacts JSONField(default=list) · photo ImageField(upload_to="students/photos/", blank) · created_at/updated_at.
- `EnrollmentEvent`: student FK(CASCADE, related_name="enrollment_events") · from_status/to_status CharField(16) · reason_code CharField(32, choices: completed, moved_city, financial, behavior, schedule_conflict, other; blank) · note TextField(blank) · actor FK(users.User, SET_NULL, null) · created_at. Ordering `("-created_at",)`.
- `StudentIdCounter`: year PositiveSmallIntegerField(unique) · last_value PositiveIntegerField(default=0).
- `ParentProfile` (apps/parents): user OneToOne(related_name="parent_profile") · workplace CharField(200, blank) · notes TextField(blank) · created_at/updated_at.
- `Guardian`: parent FK(ParentProfile, CASCADE, related_name="guardianships") · student FK("students.StudentProfile", CASCADE, related_name="guardians") · relationship CharField(16, choices mother/father/grandparent/legal_guardian/other) · is_primary Boolean(default=False) · custody_notes TextField(blank). `unique_together (parent, student)`; UniqueConstraint(fields=["student"], condition=Q(is_primary=True), name="one_primary_guardian_per_student").
- `PickupAuthorization`: student FK(CASCADE, related_name="pickup_authorizations") · full_name CharField(200) · phone CharField(32) · relationship CharField(32, blank) · is_active Boolean(default=True) · created_at.
- `TeacherProfile` (apps/teachers): user OneToOne(related_name="teacher_profile") · branch FK(org.Branch, PROTECT, related_name="teachers") · department FK(org.Department, SET_NULL, null/blank, related_name="teachers") · hire_date DateField(null/blank) · subjects JSONField(default=list) · qualifications TextField(blank) · salary_type CharField(8, choices hourly/monthly, default "monthly") · rate DecimalField(12,2, null/blank) · is_substitute Boolean(default=False) · created_at/updated_at.
- `Cohort` (apps/cohorts): name CharField(120) · branch FK(org.Branch, PROTECT, related_name="cohorts") · department FK(org.Department, SET_NULL, null/blank) · level CharField(64, blank) · start_date/end_date DateField · capacity PositiveSmallIntegerField(null/blank) · primary_teacher FK("teachers.TeacherProfile", SET_NULL, null, related_name="primary_cohorts") · default_room FK("org.Room", SET_NULL, null/blank) · is_archived Boolean(default=False, db_index) · created_at/updated_at. `unique_together (branch, name)`.
- `CohortMembership`: cohort FK(CASCADE, related_name="memberships") · student FK("students.StudentProfile", CASCADE, related_name="cohort_memberships") · start_date DateField · end_date DateField(null/blank) · moved_reason CharField(64, blank). UniqueConstraint(fields=["cohort","student"], condition=Q(end_date__isnull=True), name="one_active_membership_per_cohort_student").
- `CohortTeacher`: cohort FK(CASCADE, related_name="co_teachers") · teacher FK("teachers.TeacherProfile", CASCADE, related_name="co_teaching") · role CharField(16, default "co_teacher"). `unique_together (cohort, teacher)`.

**Endpoints** (all `required_perms` via `default_perms(resource)` + explicit `@action` mappings): `/api/v1/students/` CRUD + `POST /{id}/transition/` (students:write) + `POST /import/` (students:write) + `GET /birthdays/` (students:read) · `/api/v1/parents/` CRUD + `GET /{id}/students/` (parents:read) + nested guardian/pickup routes (parents:write) · `/api/v1/teachers/` CRUD · `/api/v1/cohorts/` CRUD + `POST /{id}/move-student/` (cohorts:write) + `GET /{id}/members/` (cohorts:read). Responses are paginated envelopes per `core/pagination.py`.

**Signals/Celery:** `cohort_member_moved` signal (audit stub — log-only consumer today, idempotent). No Celery tasks today.

**Tests required** (TESTING.md domain tier — happy/denied/cross-tenant/validation/query-count per endpoint): `test_enrollment_transition_table` (parameterized), `test_student_id_sequence_and_pattern`, `test_student_id_concurrent_no_dupes`, `test_csv_import_partial_errors`, `test_primary_guardian_unique`, `test_parent_sees_only_own_children` (queryset scoping), `test_cohort_move_keeps_history`, `test_archived_cohort_write_400`, `test_medical_notes_encrypted_at_rest`, `test_students_list_query_count`, cross-tenant 404/401 for students + cohorts via Lane E's two-tenant fixture.

**Publish to WORKLOG:** (1) profile `related_name`s (`student_profile`, `parent_profile`, `teacher_profile`) — Day-2 lanes and Lane F validation hang off these; (2) `Guardian` link shape for D2-B attendance notifications; (3) enrollment status enum values; (4) `cohort_member_moved` signal for D3-D audit; (5) seed data inventory (counts + identifiers) for everyone's manual testing.

---

## Lane E — Test foundation (merges last)

**Objective:** the shared test substrate every lane builds on for 5 days: two-tenant session fixtures, factories, JWT client helpers, THE tenant-isolation test (written FIRST, red until Lane C lands TD-1), permission-matrix harness skeleton. Implements TASKS §26 (start); TD-1 (proof), TD-16 (`factory-boy`), TD-20.

Read first: `pytest.ini` (testpaths=`apps`, `--reuse-db`, settings=`config.settings.test`) and `config/settings/test.py` (Celery eager, in-memory channels, locmem cache, MD5 hasher).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D1-LE-1 | Root `conftest.py` + `tests/` package: session-scoped two-tenant fixtures `tenant_a` (slug `alfa`, host `alfa.testserver`) and `tenant_b` (slug `beta`, host `beta.testserver`) created once via `provision_center` under `django_db_blocker.unblock()`; `pytest.ini` testpaths gains ` tests` (additive); add `factory-boy` to `pyproject.toml` | `conftest.py` (new, repo root), `tests/__init__.py`, `pytest.ini`, `pyproject.toml` | `uv run pytest tests -q` collects; both schemas exist once per session (fast — no per-test re-provision); `--reuse-db` still works | A merged |
| D1-LE-2 | Factories: `tests/factories.py` — `UserFactory` (phone sequence `+99890XXXXXXX`), `BranchFactory`, `DepartmentFactory`, `RoleMembershipFactory(role=...)`; schema-aware (document: call inside `schema_context(tenant.schema_name)`) | `tests/factories.py` (new) | Each factory creates a valid row inside a tenant schema; used by at least one passing test | D1-LE-1 |
| D1-LE-3 | API client helpers: `tests/clients.py` — `api_client_for(user, tenant)` returns DRF `APIClient` with `HTTP_HOST=<tenant primary domain>` and `Authorization: Bearer <access>` minted inside that tenant's `schema_context` via `apps.auth.services.issue_token_pair`; plus `anon_client(tenant)` | `tests/clients.py` (new) | Helper used by isolation test; minting works pre- and post-TD-1 (claims simply ignored before) | D1-LE-2 |
| D1-LE-4 | **THE tenant-isolation test** (TASKS §26 item 1 — write FIRST, commit red on your branch): token minted on `tenant_a` + request to `GET /api/v1/users/me/` with `tenant_b` host → expect 401 with `error.code == "tenant_mismatch"`. Coordinate via WORKLOG: keep `@pytest.mark.xfail(reason="TD-1 pending", strict=False)` ONLY if Lane C has not merged by your merge time; otherwise it runs strict | `tests/test_tenant_isolation.py` (new) | Green on master at EOD (Lane C merges before you); also asserts the same token works on `tenant_a` (sanity) | D1-LE-3, C merged |
| D1-LE-5 | Permission-matrix harness skeleton: `tests/test_permission_matrix.py` — parameterized over (role, endpoint, method, expected) driven by a declarative `MATRIX_CASES` list; Day-1 coverage: users, org, students, cohorts endpoints × roles director/teacher/student/registrar/it; designed so Day-2+ lanes append cases only | `tests/test_permission_matrix.py` (new) | ≥ 20 parameterized cases pass; adding a case is a one-line append (prove with a comment template); fail-closed verified: unmapped action → 403 | C, D, F merged |
| D1-LE-6 | Channels + Celery plumbing: `tests/test_plumbing.py` — (a) `WebsocketCommunicator` against the app in `config/asgi.py`: anonymous connect to `/ws/ping/` rejected, JWT-authed connect accepted (uses `infrastructure/websocket` middleware); (b) Celery-eager proof: `purge_expired_otps` runs synchronously and deletes an expired OTP row in `tenant_a` | `tests/test_plumbing.py` (new) | Both pass under `config.settings.test` (InMemory channel layer, eager Celery); documents the pattern for D4-C | D1-LE-3 |
| D1-LE-7 | OTP flow tests (TASKS §26): request→verify happy path with MockEskiz; throttle 429 on 4th/min; 5 wrong codes invalidates (uses existing `OTP_MAX_ATTEMPTS=5`); refresh rotation blacklists old token | `tests/test_auth_flows.py` (new) | All four scenarios green; no real SMS attempted (`ESKIZ_USE_MOCK=True` asserted) | D1-LE-3, C merged |

**Models/Endpoints/Signals:** none — this lane ships only test infrastructure.

**Tests required:** this lane IS the tests. Floor contribution: the suite above + other lanes' tests must reach **≥ 70% coverage** (TD-20) — check with `uv run pytest -q --cov=apps --cov=core` before merging; if short, the gap-filling burden is yours (target untested `core/` modules first).

**Publish to WORKLOG:** (1) fixture names + import paths (`tenant_a`, `tenant_b`, `api_client_for`, factories) — every lane's tests MUST use these, not hand-rolled clients; (2) isolation test status (red/green + when); (3) `MATRIX_CASES` append-format for Day-2 lanes; (4) measured coverage % at merge.

---

## Cross-lane integration points (Day 1)

| Producer → Consumer | Interface | When |
|---|---|---|
| A → all | Migration graph, `core/middleware.py`, `_registration_open()` hook | A merges first; everyone rebases + regenerates migrations |
| C → E | TD-1 `tenant_mismatch` 401 makes isolation test green | E flips xfail off after C's WORKLOG announcement |
| C → B, D, F | `required_perms` + `default_perms()` contract for every new/edited viewset | B/D/F written against it from the start |
| B → C | `CenterSettings.otp_cooldown_seconds` replaces settings fallback | B wires in D1-LB-4/C keeps fallback for safety |
| B → D | `CenterSettings.student_id_pattern` + `center_code`; `open_registration` | D reads via `CenterSettings.load()` |
| B → F | Shared `apps/org/models.py` — B appends `CenterSettings`, F appends org models | F merges after B; F resolves org migration numbering |
| F → D | `org.Room` (Cohort.default_room), `Department.head` validation contract | D merges after F; D un-skips F's head-validation test |
| D → F | `TeacherProfile` exists → F's late validation goes strict automatically | No code change needed in F (apps.get_model) |
| D, F, C → E | Endpoints + roles for the permission-matrix harness | E merges last, harness covers the day's surface |

Merge order recap: **A → C → B → F → D → E**. Each lane: rebase on master, regenerate own migrations, `makemigrations --merge` only if CI's `manage.py check`/migrate complains about numbering.

## EOD gate — all boxes checked before Day 1 closes

- [ ] `uv run ruff check . && uv run ruff format --check .` clean on master
- [ ] `uv run mypy apps core infrastructure config` clean on master
- [ ] `uv run pytest -q --cov=apps --cov=core --cov-fail-under=70` green on master (TD-20 floor 70)
- [ ] CI: all 4 jobs (lint, typecheck, test, schema) green on master; coverage gate active in `.github/workflows/ci.yml`
- [ ] Fresh-clone proof: `docker compose -f docker/docker-compose.yml up -d postgres redis minio` → `uv run python manage.py migrate_schemas` → `uv run python scripts/seed_dev.py` all exit 0
- [ ] Demo script (run manually, paste outputs into WORKLOG):
  1. `curl -H "Host: x" localhost:8000/healthz/live` → 200; `/healthz/ready` → 200; response carries `X-Request-ID`
  2. Provision a second center via `uv run python scripts/create_tenant.py` (slug `acme`); reserved slug `admin` rejected with `slug_reserved`
  3. OTP login on `demo.localhost`: request → verify → `{access, refresh}`; decoded access shows `schema: "demo"`, `tv`, `roles`
  4. Same access token against `acme.localhost` → 401 `tenant_mismatch` (ROADMAP §7 acceptance #1)
  5. Unknown phone OTP verify → 400 `user_not_found` (open_registration off)
  6. Apex `localhost:8000/admin/` platform-staff login works (TD-3)
  7. `PATCH /api/v1/org/settings/` as director changes `late_threshold_minutes`; as teacher → 403
  8. Create teacher → cohort (with `default_room`) → student → transition lead→…→active → student_id matches `DEMO-2026-0000N` → link parent as primary guardian → move student to second cohort, history retained
  9. Set demo `is_active=False` → API 503 `center_inactive`; revert
  10. `POST /api/v1/auth/logout-all/` → old refresh AND access both rejected
- [ ] TASKS.md ticked: §0 all, §1 (shipped subset — leave Prometheus/schema-diff/CODEOWNERS unticked with `[~]` deferral notes), §2 (Day-1 subset), §3 (Day-1 subset), §4 all, §5–§8 (Day-1 subset), §26 Day-1 items
- [ ] WORKLOG: one entry per lane (format per `agents/WORKLOG.md`) including the "Publish to WORKLOG" items above, test counts, coverage %, and explicit handoff notes for Day-2 lanes (schedule needs Room+working hours; attendance needs Guardian links; academics needs TeacherProfile)
- [ ] No secrets committed; all `[OWNER:O-x]` touchpoints today (O-1, O-2, O-7, O-8, O-10, O-11) are mock/env-gated and logged in `agents/OWNER-ACTIONS.md` status if state changed
