# DAY 5 — Hardening, speed & frontend handoff

**Mission.** At start of day the platform is feature-complete: people/academics/money/AI/realtime/control-center all live, coverage ≥ 80%, paywall enforcing, i18n done. Today nothing new is *featured* — everything is hardened, measured, documented, and handed off. At EOD: security posture locked (axes/CSP/CORS/encryption verified), test suite ≥ 85% with the full §26 matrix, every list endpoint < 150 ms p95 with zero N+1, OpenAPI polished with generated TS + Dart clients, ADRs + runbooks + prod compose written, the full ROADMAP §7 acceptance script green, and `v1.0.0` tagged.

---

## Prerequisites (verify before starting any lane)

Read the last 2 days of `agents/WORKLOG.md`. Day 4 must have landed: AI budgets + Celery-only AI (D4-A), reports incl. scheduled (D4-B), Channels consumers `NotificationConsumer`/`AttendanceConsumer` (D4-C), printing server side (D4-D), control center + impersonation (D4-E), i18n + consolidated beat schedule (D4-F).

Smoke on `master` before branching (all must pass; if red, fixing master is your first task — log it):

```bash
uv run pytest -q                                  # green, coverage >= 80
uv run ruff check . && uv run mypy apps core infrastructure config
docker compose -f docker/docker-compose.yml up -d
uv run python manage.py migrate_schemas && uv run python scripts/seed_dev.py
curl -s http://demo.localhost:8000/healthz/ready          # 200
curl -s http://localhost:8000/api/v1/platform/resolve/?slug=demo   # 200 (TD-19; if 404, flag D5-D task D5-D-8)
uv run python manage.py spectacular --file /tmp/s.yaml --validate  # exits 0
```

Merge order today (ROADMAP §2.3): **A → B → C → D → E → F**. Lane F merges last and runs the final E2E on merged master.

---

## Lane A — Security hardening (TASKS §25)

**Objective.** Close every §25 item: brute-force lockout on admin, CSP, production CORS allowlist, security headers verified by tests, TD-11 encryption verified at rest, rotation runbooks, throttle coverage on all public endpoints, dependency audit in CI. Implements TASKS §25, §1 (rotation runbook item), §3 (throttle items). TDs: TD-2, TD-4, TD-9, TD-11, TD-16 (`django-axes`, `django-csp`), TD-18.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D5-A-1 | django-axes on admin login: add `axes` to both `SHARED_APPS` and `TENANT_APPS`, prepend `axes.backends.AxesStandaloneBackend` to `AUTHENTICATION_BACKENDS`, append `axes.middleware.AxesMiddleware` to `MIDDLEWARE`. Settings: `AXES_FAILURE_LIMIT=5`, `AXES_COOLOFF_TIME=timedelta(minutes=15)`, `AXES_LOCKOUT_PARAMETERS=[["username","ip_address"]]`; `AXES_ENABLED=False` in `config/settings/test.py` except the lockout test (override). Generate migration. | `config/settings/base.py`, `config/settings/test.py`, `pyproject.toml` | 6th failed `/admin/login/` POST with same username+IP returns 429/lockout page on both apex and tenant admin; JWT API login flow unaffected (OTP path bypasses axes) | — |
| D5-A-2 | django-csp: add `csp.middleware.CSPMiddleware`; policy `default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'` (inline allowances exist only for admin + swagger-ui; documented in the policy comment) | `config/settings/base.py` | `Content-Security-Policy` header present on `/admin/login/` and `/api/schema/swagger-ui/`; swagger-ui still renders; API JSON responses carry the header | — |
| D5-A-3 | Kill permissive CORS in prod: confirm `CORS_ALLOW_ALL_ORIGINS = True` exists ONLY in `config/settings/development.py`; in `config/settings/production.py` add a startup assertion that `CORS_ALLOWED_ORIGINS` is non-empty and contains no `*` | `config/settings/production.py` | `DJANGO_SETTINGS_MODULE=config.settings.production SECRET_KEY=x CORS_ALLOWED_ORIGINS= uv run python manage.py check` raises `ImproperlyConfigured`; with a real origin list it passes | — |
| D5-A-4 | Security headers test pass: assert `X-Content-Type-Options: nosniff` (`SECURE_CONTENT_TYPE_NOSNIFF=True` explicit in base), `X-Frame-Options: DENY` (move `X_FRAME_OPTIONS="DENY"` from production-only into base), HSTS settings present in production settings module | `config/settings/base.py`, `apps/audit/tests/` or `tests/security/test_headers.py` | `test_security_headers_on_api_response` asserts all three headers on a tenant API GET | D5-A-2 |
| D5-A-5 | TD-11 rollout verification: grep audit confirming `core/fields.py` `EncryptedCharField`/`EncryptedTextField` used on `users.User.national_id`, `students.StudentProfile.medical_notes`, all credential fields on `apps/payments` `ProviderConfig`, and the Soliq token field (`apps/payments` fiscal config). Write encryption-at-rest test: raw `connection.cursor()` SELECT shows ciphertext (Fernet `gAAAA` prefix), not plaintext | `tests/security/test_field_encryption.py` | `test_national_id_ciphertext_at_rest`, `test_provider_config_ciphertext_at_rest` pass; a grep for `national_id|medical_notes` finding a plain `CharField`/`TextField` is a bug — fix in-lane | — |
| D5-A-6 | `FIELD_ENCRYPTION_KEY` rotation runbook: multi-key decrypt/single-key encrypt procedure, re-encrypt management command outline, key storage guidance [OWNER:O-11] | `docs/runbooks/key-rotation.md` | Runbook has numbered steps incl. rollback; references the exact settings key `FIELD_ENCRYPTION_KEY` and affected models from D5-A-5 | D5-A-5 |
| D5-A-7 | Secrets rotation runbook: `SECRET_KEY` (session/CSRF impact), Eskiz [OWNER:O-1], Anthropic [OWNER:O-2], Click/Payme/Uzum per-tenant `ProviderConfig` [OWNER:O-3][OWNER:O-4][OWNER:O-6], Soliq [OWNER:O-5], S3 keys [OWNER:O-9] | `docs/runbooks/secrets-rotation.md` | One section per secret with: where it lives, rotation steps, verification command, blast radius | — |
| D5-A-8 | Throttle review of every unauthenticated endpoint: OTP scopes exist (`otp_phone 3/min`, `otp_ip 10/min`, `otp_global 1000/hour` in `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`); ADD scoped throttles: `webhook: 120/min` on `/api/v1/webhooks/<provider>/<center_slug>/`, `platform_resolve: 30/min` on `GET /api/v1/platform/resolve/`, `token_refresh: 30/min` on the refresh endpoint. Every throttle response uses the TD-18 envelope (code `throttled`) | `config/settings/base.py`, `apps/payments/views.py` (webhook views), `apps/tenancy/views.py`, `apps/auth/views.py` | `tests/security/test_throttles.py::test_webhook_throttled`, `::test_resolve_throttled`, `::test_refresh_throttled` each assert 429 + envelope after exceeding the rate | — |
| D5-A-9 | Admin action audit verification (TD-9): confirm D3-D wired django-admin `LogEntry` (or equivalent receiver) into `apps/audit` `audit_log()`; if missing, add a `post_save` receiver on `django.contrib.admin.models.LogEntry` in `apps/audit/receivers.py` | `apps/audit/receivers.py`, `apps/audit/tests/` | `test_admin_change_writes_audit_log`: editing a User in admin produces an `AuditLog` row with actor + resource | — |
| D5-A-10 | Pen-test scope document: assets in scope (apex + tenant APIs, WS, webhooks, admin), out of scope (provider sandboxes), test accounts, known-accepted risks | `docs/security/pentest-scope.md` | Doc lists every public URL prefix from `config/urls.py` and `config/urls_public.py`; reviewed by Lane F before EOD | — |
| D5-A-11 | Dependency audit in CI: new `audit` job — `uv export --frozen --no-emit-project -o requirements-audit.txt` then `uvx pip-audit -r requirements-audit.txt`. Triage findings; pin/upgrade fixable ones (justify in WORKLOG per TD-16) | `.github/workflows/ci.yml` | CI `audit` job green on the lane branch; any unfixable advisory documented in `docs/security/pentest-scope.md` accepted-risks | — |

**Models to create:** none. axes ships its own `AccessAttempt`/`AccessLog` tables — they land in both schemas via SHARED+TENANT membership; verify with `uv run python manage.py makemigrations --check --dry-run` (no app-local migrations expected).

**Endpoints to expose:** none new. This lane only changes settings, headers, throttles, docs.

**Signals/Celery tasks:**
- D5-A-9 receiver on `django.contrib.admin.models.LogEntry` `post_save` → `apps.audit.services.audit_log()`. Idempotent: append-only insert, one AuditLog per LogEntry pk (guard with `get_or_create` on a `source_id` field if AuditLog has one; otherwise accept at-least-once since receivers fire once per save).
- No new Celery tasks.

**Tests required** (TESTING.md security matrix rows):
- `tests/security/test_headers.py::test_security_headers_on_api_response`
- `tests/security/test_axes.py::test_admin_lockout_after_5_failures` (with `AXES_ENABLED=True` override)
- `tests/security/test_field_encryption.py::test_national_id_ciphertext_at_rest`, `::test_provider_config_ciphertext_at_rest`
- `tests/security/test_throttles.py::test_webhook_throttled`, `::test_resolve_throttled`, `::test_refresh_throttled`
- `apps/audit/tests/test_admin_audit.py::test_admin_change_writes_audit_log`

**Publish to WORKLOG:**
- New throttle scope names + rates (`webhook`, `platform_resolve`, `token_refresh`) — Lane D documents them, Lane F asserts the 429 envelope.
- axes settings keys and the test-settings override pattern.
- Standing rule: every new public (unauthenticated) endpoint MUST declare a throttle scope.
- Paths of `docs/runbooks/key-rotation.md` and `docs/runbooks/secrets-rotation.md`.

---

## Lane B — Test completion to ≥ 85% (TASKS §26)

**Objective.** Make every §26 line a named, passing test; add the parameterized permission matrix, migration tests, Celery schema isolation, Channels auth; close coverage gaps surfaced by the report; raise the CI floor to 85 (TD-20). TDs: TD-1, TD-4, TD-5, TD-20.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D5-B-1 | §26 audit: map each of the 16 §26 items to an existing test (Day 1–4 lanes wrote most). Produce the mapping table in your WORKLOG entry. Write any missing ones | `apps/*/tests/`, `tests/` | Every §26 checkbox maps to ≥ 1 named test that passes; §26 ticked in TASKS.md | — |
| D5-B-2 | Full permission matrix test: parameterize over (role in `core.permissions.Role.ALL`) x (every registered viewset's `required_perms` resource:verb). Expected allow/deny computed via `core.permissions.has_permission_code`; actual via APIClient with a user holding exactly one `RoleMembership`. Covers TD-4 fail-closed: a viewset action with no mapping must 403 | `tests/permissions/test_matrix.py` | Test count ≥ 12 roles x distinct (resource, verb) pairs; zero xfails; a deliberate temporary removal of one `required_perms` entry makes the matrix fail (verify locally, then restore) | — |
| D5-B-3 | Object-scope test: teacher with membership in branch A gets 404/403 grading a student in branch B (`ObjectScopedPermission` + selector scoping) | `tests/permissions/test_object_scope.py` | `test_teacher_branch_a_cannot_grade_branch_b` passes | D5-B-2 |
| D5-B-4 | Migration tests: (a) fresh DB — create a scratch database via cursor on the postgres connection, run `call_command("migrate_schemas", shared_only=True)` against it, assert success, drop it; (b) new Center — `provision_center(...)` in test, then `schema_context` + `connection.introspection.table_names()` contains `users_user`, `students_studentprofile` | `tests/migrations/test_migration_graph.py` | `test_fresh_shared_migrate_succeeds`, `test_new_center_auto_runs_tenant_migrations` pass in CI (needs the postgres service — already in `ci.yml` test job) | — |
| D5-B-5 | Celery schema isolation: schedule a real task (e.g. a finance or notification task) with `_schema_name="tenant_a"` while two tenants exist; assert the row it writes lands in tenant_a's schema only. Note `config/settings/test.py` sets `CELERY_TASK_CLS` to plain Task — this test overrides settings to use `tenant_schemas_celery.task:TenantTask` with eager mode | `tests/celery/test_schema_isolation.py` | `test_task_runs_under_scheduling_tenant_schema` passes; tenant_b row count unchanged | — |
| D5-B-6 | Channels auth tests: anonymous WS connect to `/ws/notifications/` rejected (close code 4401 or denied), JWT-authenticated connect accepted and receives the hello/connected frame; token with wrong `schema` claim rejected (TD-1 on WS path via `infrastructure/websocket/middleware.py`) | `tests/channels/test_ws_auth.py` | 3 named tests pass with `InMemoryChannelLayer` | — |
| D5-B-7 | Coverage gap-fill: run `uv run pytest --cov=apps --cov=core --cov-report=term-missing`; write tests for the 10 worst-covered modules (typically: services error branches, webhook edge cases, selectors filters) until total ≥ 86% (1% buffer over the gate) | lowest-coverage modules' test files | Local coverage ≥ 86%; no module under 60% except generated/admin stubs | D5-B-1..6 |
| D5-B-8 | Raise the gate: change `--cov-fail-under` from 80 → **85** everywhere it appears (`.github/workflows/ci.yml` test job; `pytest.ini` addopts if Day 1 put it there). This is your LAST commit on the branch | `.github/workflows/ci.yml`, `pytest.ini` | CI green at 85 on the lane branch; announce in WORKLOG before merging so lanes C–F know the new floor | D5-B-7 |

**Models to create / Endpoints to expose / Signals/Celery:** none — this lane only adds tests and the CI gate.

**Tests required** — the §26 checklist with canonical names (verify-or-write each; most exist from Days 1–4):
- [ ] §26.1 tenant isolation → `tests/test_isolation/test_tenant_isolation.py::test_jwt_from_tenant_a_rejected_on_tenant_b` (Day 1, verify)
- [ ] §26.2 OTP happy path → `apps/auth/tests/test_otp.py::test_otp_request_verify_happy_path`
- [ ] §26.3 OTP throttle → `apps/auth/tests/test_otp.py::test_fourth_otp_request_in_minute_returns_429`
- [ ] §26.4 OTP 5 wrong codes → `apps/auth/tests/test_otp.py::test_five_wrong_codes_invalidates_otp`
- [ ] §26.5 refresh rotation → `apps/auth/tests/test_jwt.py::test_old_refresh_blacklisted_after_rotation`
- [ ] §26.6 refresh reuse → `apps/auth/tests/test_jwt.py::test_blacklisted_refresh_reuse_revokes_all_user_refreshes`
- [ ] §26.7 phone-or-email login → `apps/auth/tests/test_login.py::test_login_with_phone`, `::test_login_with_email`
- [ ] §26.8 permission matrix → `tests/permissions/test_matrix.py` (D5-B-2)
- [ ] §26.9 object scope → `tests/permissions/test_object_scope.py::test_teacher_branch_a_cannot_grade_branch_b` (D5-B-3)
- [ ] §26.10–11 channels → `tests/channels/test_ws_auth.py` (D5-B-6)
- [ ] §26.12 celery isolation → `tests/celery/test_schema_isolation.py::test_task_runs_under_scheduling_tenant_schema` (D5-B-5)
- [ ] §26.13–14 migrations → `tests/migrations/test_migration_graph.py` (D5-B-4)
- [ ] §26.15 OpenAPI generation → CI `schema` job (already exists; cite the run)
- [ ] §26.16 coverage threshold → D5-B-8 (now 85, superseding the §26 text's 70 per TD-20)

**Publish to WORKLOG:**
- The §26 → test-name mapping table above, with pass status per item.
- The new coverage floor (85) and exactly which files carry the number, BEFORE merging.
- Production bugs found while writing tests: fix `[out-of-lane]` if trivial, otherwise hand to the owning lane same-day via WORKLOG.

---

## Lane C — Performance: query audit, indexes, perf smoke, caching, queue split

**Objective.** Prove and enforce DoD #12: every list endpoint paginated, < 150 ms p95 locally on scaled seed data, zero N+1. Add the measurement tooling (scaled seed, perf smoke script), fix what it finds, split Celery queues. Implements ROADMAP §3 DoD items 3/12, TASKS §22 (worker concurrency), §3 (permission cache). TDs: TD-13 (CenterSettings cache), TD-20.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D5-C-1 | Query-count audit: for EVERY list endpoint registered under `config/urls.py` prefixes (students, parents, teachers, cohorts, schedule, attendance, academics, assignments, content, printing, finance, payments, notifications, ai, audit, reports, org, users/devices), add/verify a `django_assert_max_num_queries` test against ≥ 25 seeded rows. Budget: ≤ 8 queries per list call (auth + perms + count + page + prefetches) | `apps/<app>/tests/test_queries.py` per app | One `test_<resource>_list_query_count` per list endpoint; all pass; the per-endpoint budget is asserted, not just "no explosion" | — |
| D5-C-2 | Fix every N+1 found by D5-C-1 in `selectors.py` (`select_related`/`prefetch_related`), never in views | `apps/*/selectors.py` | Re-run D5-C-1 green; diff touches selectors only | D5-C-1 |
| D5-C-3 | Scaled seed: add `--scale N` flag to `scripts/seed_dev.py` (argparse). `--scale 1` (default) = current demo; `--scale 10` = 1000 students, 50 cohorts, 30 teachers, a 12-week term of materialized lessons (TD-12), attendance for past weeks, 1 invoice/student. Use services for correctness, `bulk_create` where services are too slow; idempotent (skip if counts already met) | `scripts/seed_dev.py` | `uv run python scripts/seed_dev.py --scale 10` finishes < 5 min and prints final counts; rerun is a no-op | — |
| D5-C-4 | Index sweep: with `--scale 10` data, run `EXPLAIN ANALYZE` (via `python manage.py dbshell` in the tenant schema) on every selector's base query filtered the way the API filters it. Any seq-scan on a filtered column gets `db_index=True`/`Index(fields=[...])` + migration | `apps/*/models.py` + migrations | WORKLOG lists each query examined and verdict; zero seq-scans on list-endpoint filter columns at scale 10 | D5-C-3 |
| D5-C-5 | Perf smoke script: `scripts/perf_smoke.py` — logs in via OTP mock as director, hits the top-20 list/detail endpoints (the D5-C-1 set) 30x each against `http://demo.localhost:8000`, discards first warmup call, computes p50/p95 per endpoint, prints a table, exits 1 if any p95 > 150 ms | `scripts/perf_smoke.py` | `uv run python scripts/perf_smoke.py` exits 0 against `--scale 10` data on the dev compose stack; output table pasted into WORKLOG | D5-C-3, D5-C-2 |
| D5-C-6 | Cache verification: (a) per-request memoization of `core.permissions._user_roles` (Day 1 item, TASKS §3) — assert a request triggering 3 permission checks does 1 RoleMembership query; (b) `CenterSettings` read-through Redis cache keyed `center_settings:{schema_name}` with invalidation on save (post_save receiver or `save()` override in `apps/tenancy` — wherever D1-B put the model); add if missing | `core/permissions.py`, `apps/tenancy/` (CenterSettings module), `tests/perf/test_caching.py` | `test_user_roles_memoized_per_request`, `test_center_settings_cached`, `test_center_settings_cache_invalidated_on_save` pass | — |
| D5-C-7 | Pagination sanity: `core/pagination.py` `DefaultPagination` — confirm `page_size=25`, add `max_page_size=100` if absent; verify no list endpoint disables pagination | `core/pagination.py`, grep check | `test_page_size_capped_at_100` passes; grep for `pagination_class = None` returns only deliberate, documented cases | — |
| D5-C-8 | Celery queue split: `CELERY_TASK_DEFAULT_QUEUE="default"`; `CELERY_TASK_ROUTES` routing SMS/email/push/webhook-followup tasks → `io`, AI tasks (`apps.ai` + `infrastructure/ai`) → `ai`, everything else → `default`. Compose: replace single `celery-worker` with `celery-worker-default` (`-Q default --concurrency=4`), `celery-worker-io` (`-Q io --concurrency=8`), `celery-worker-ai` (`-Q ai --concurrency=2`); comment the rationale (io = network-bound, ai = budget/rate-bound) | `config/settings/base.py`, `docker/docker-compose.yml` | `celery -A config inspect registered` shows tasks on correct queues; `docker compose up` runs 3 workers; a mock SMS dispatch lands on `io` (assert via test with `CELERY_TASK_ROUTES` applied and `.apply_async` queue kwarg captured) | — |

**Endpoint set for D5-C-1 audit and D5-C-5 perf smoke** (the "top 20" — list action of each router under these `config/urls.py` prefixes, plus the two non-list hot paths):

```
/api/v1/students/            /api/v1/parents/             /api/v1/teachers/
/api/v1/cohorts/             /api/v1/schedule/   (lessons)  /api/v1/attendance/  (records)
/api/v1/academics/  (exams + grades)             /api/v1/assignments/  (+ submissions)
/api/v1/content/   (files)   /api/v1/printing/   (jobs)    /api/v1/finance/  (invoices)
/api/v1/payments/  (payments) /api/v1/notifications/       /api/v1/ai/       (requests)
/api/v1/audit/     (logs)    /api/v1/reports/              /api/v1/org/      (branches)
/api/v1/users/devices/       /api/v1/users/me/   (detail)  student dashboard endpoint (detail)
```
Use the actual router basenames as registered by Days 1–4; if an app exposes multiple routers, audit each list action — the "20" is a floor, not a cap.

**Models to create:** none — index-only migrations from D5-C-4 (each justified by an EXPLAIN result quoted in the migration's docstring or WORKLOG).

**Endpoints to expose:** none new.

**Signals/Celery tasks:**
- CenterSettings cache invalidation receiver (`post_save` → `cache.delete(f"center_settings:{schema_name}")`). Idempotent: cache deletes are safe to repeat; key includes schema so cross-tenant invalidation is impossible.
- No new tasks; D5-C-8 is routing + worker topology only.

**Tests required** (TESTING.md performance matrix row):
- `apps/<app>/tests/test_queries.py::test_<resource>_list_query_count` — one per endpoint in the set above, each with an explicit `django_assert_max_num_queries(8)`.
- `tests/perf/test_caching.py::test_user_roles_memoized_per_request`, `::test_center_settings_cached`, `::test_center_settings_cache_invalidated_on_save`
- `tests/perf/test_pagination.py::test_page_size_capped_at_100`
- `tests/perf/test_celery_routing.py::test_sms_task_routes_to_io_queue`

**Publish to WORKLOG:**
- The perf table (p50/p95 per endpoint at `--scale 10`) — Lane F pastes it as item-12 evidence.
- Index migrations added, so later mergers renumber correctly.
- Queue names + routing rules (`default`/`io`/`ai`) — Lane E mirrors them in compose-prod, Lane F's E2E exercises the split workers.
- The `seed_dev.py --scale` contract (what each scale level creates).

---

## Lane D — API contract, OpenAPI polish, TS + Dart clients (TASKS §27 handoff)

**Objective.** Make the generated schema the real frontend contract: every endpoint tagged/summarized/exampled, enums named, errors documented; commit a canonical `openapi.yaml` with a CI diff guard; generate and compile-check TypeScript and Dart clients; finalize `agents/API-CONTRACT.md` with real captured responses; document WS + webhook contracts. TDs: TD-18, TD-19, TD-2.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D5-D-1 | `@extend_schema` sweep: every view/action across all apps has summary, tags (one tag per app, Title Case), at least one request+response example, and documented error responses (401/403/404/422/429 with the TD-18 envelope). Add a shared `ERROR_RESPONSES` dict in `core/openapi.py` (new file) reused by all apps | `core/openapi.py`, `apps/*/views.py` | `manage.py spectacular --validate` exits 0 with **zero** warnings (capture stderr); spot-check: swagger-ui shows examples on students list + payments webhook | — |
| D5-D-2 | Enum naming: populate `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]` for every colliding/auto-named enum (statuses: enrollment, payment, print job, subscription, attendance, report run); add `SPECTACULAR_SETTINGS["TAGS"]` with one entry + description per app | `config/settings/base.py` | Generated schema contains no `Enum`-suffixed auto names like `Status1b2Enum`; `grep -c "Enum'" openapi.yaml` reviewed in WORKLOG | D5-D-1 |
| D5-D-3 | Canonical schema + CI diff guard: commit `openapi.yaml` at repo root; extend the existing `schema` job in `.github/workflows/ci.yml`: after generation, `git diff --exit-code openapi.yaml` — fail with message "schema changed: regenerate openapi.yaml and clients" | `openapi.yaml`, `.github/workflows/ci.yml` | CI fails on a branch that changes an endpoint without regenerating; passes when regenerated | D5-D-1, D5-D-2 |
| D5-D-4 | TypeScript client: **decision — `openapi-typescript` + `openapi-fetch`** (types-only generation, deterministic, pairs with TanStack Query per TASKS §27). `clients/typescript/package.json` with scripts `generate` (`npx openapi-typescript@7 ../../openapi.yaml -o src/schema.d.ts`) and `check` (`tsc --noEmit`); commit generated `src/schema.d.ts` | `clients/typescript/` | `npm install && npm run generate && npm run check` exits 0 in `clients/typescript/`; command documented in the package README | D5-D-3 |
| D5-D-5 | Dart client: `openapi-generator` with generator `dart-dio` via docker: `docker run --rm -v ${PWD}:/local openapitools/openapi-generator-cli generate -i /local/openapi.yaml -g dart-dio -o /local/clients/dart`; verify `dart pub get && dart run build_runner build && dart analyze` exits 0 | `clients/dart/`, `clients/dart/README.md` | Generation + analyze green locally; exact commands in `clients/dart/README.md`; add CI `clients` job running both D5-D-4 and D5-D-5 checks (use `dart-lang/setup-dart` + node 20) | D5-D-3 |
| D5-D-6 | Finalize `agents/API-CONTRACT.md`: every documented example response replaced by a real captured response (`curl` against local seeded stack, copy-paste verbatim incl. pagination envelope and error envelope). Add auth flow, tenancy headers/subdomain rules, throttle scopes (from D5-A-8 WORKLOG entry) | `agents/API-CONTRACT.md` | Zero hand-written JSON examples remain; each example has a comment line with the curl that produced it | D5-D-1 |
| D5-D-7 | Non-OpenAPI contracts: add API-CONTRACT.md sections for (a) WebSocket — URLs `/ws/notifications/`, `/ws/cohorts/<id>/attendance/`, auth (JWT in query/header per `infrastructure/websocket/middleware.py`), message frames, close codes, reconnect-with-backoff guidance (TASKS §21); (b) webhooks — `/api/v1/webhooks/<provider>/<center_slug>/` request/response shapes per provider, signature headers, idempotency semantics | `agents/API-CONTRACT.md` | Frames documented match actual consumer code in `infrastructure/websocket/consumers.py` + app consumers; Payme JSON-RPC method list matches `apps/payments` handlers | — |
| D5-D-8 | TD-19 tenant discovery: verify `GET /api/v1/platform/resolve/?slug=demo` exists on the public schema (D4-E may have shipped it with the control center); if not, implement in `apps/tenancy`: `AllowAny` + `platform_resolve` throttle scope, response `{name, base_url, ws_url, logo, locale}`. Document in API-CONTRACT.md mobile section | `apps/tenancy/views.py`, `apps/tenancy/urls.py`, `agents/API-CONTRACT.md` | `curl http://localhost:8000/api/v1/platform/resolve/?slug=demo` returns the 5-key shape; unknown slug → 404 envelope; test `test_resolve_returns_tenant_endpoints` | coordinates with D5-A-8 (throttle scope) |
| D5-D-9 | API collection export: **decision — Postman collection** generated via `npx openapi-to-postmanv2 -s openapi.yaml -o clients/postman/collection.json -p`; commit it; regeneration command in `clients/postman/README.md` | `clients/postman/` | Collection imports cleanly into Postman/Bruno; covered by the same regenerate-on-schema-change rule as D5-D-3 | D5-D-3 |

**Models to create:** none.

**Endpoints to expose:**
- `GET /api/v1/platform/resolve/?slug=<slug>` — public schema, permission `AllowAny` + `platform_resolve` throttle scope — 200 `{name, base_url, ws_url, logo, locale}`; 404 TD-18 envelope (`code: "not_found"`) for unknown/inactive slugs (do not leak existence of suspended centers beyond 404).

**Signals/Celery tasks:** none.

**Tests required:**
- `apps/tenancy/tests/test_resolve.py::test_resolve_returns_tenant_endpoints`, `::test_resolve_unknown_slug_404`
- Schema validity and drift are CI-enforced (D5-D-3); TS/Dart compile checks live in the CI `clients` job, not pytest.

**Publish to WORKLOG:**
- The regeneration ritual as one copy-pasteable sequence: `manage.py spectacular --file openapi.yaml --validate` → `npm run generate && npm run check` (clients/typescript) → openapi-generator dart-dio command → `npx openapi-to-postmanv2 ...`. **Every endpoint change after this lane lands must run the full sequence or CI fails.**
- Final tag list (one per app) and chosen generator versions (`openapi-typescript@7`, `openapi-generator-cli` image tag).
- Confirmation that `agents/API-CONTRACT.md` examples are captured-from-reality, with the seed state they assume.

---

## Lane E — Docs, ADRs, runbooks, deploy prep (TASKS §29, §30)

**Objective.** Write the seven ADRs, per-app READMEs, operational runbooks, the production compose profile, the deploy checklist, and CHANGELOG v1.0.0. Hosting itself is [OWNER:O-9]; DNS/TLS is [OWNER:O-8] — everything here must be executable the day those land. TDs: TD-3 (ADR-007), TD-2, TD-14.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D5-E-1 | ADRs (context/decision/consequences, ≤ 60 lines each): ADR-001 schema-per-tenant via django-tenants; ADR-002 JWT-everywhere (+ TD-1 tenant binding); ADR-003 separate students/parents/teachers role apps, no cross role-app FKs; ADR-004 print agent in separate repo (TASKS §28 note); ADR-005 dropped repository/dto/interfaces layers from `core/`; ADR-006 language priority uz > en > ru (matches `LANGUAGES` in `config/settings/base.py`); ADR-007 public-schema users for platform staff (TD-3) | `docs/adr/ADR-001.md` … `ADR-007.md` | Each ADR states the decision, ≥ 2 rejected alternatives, and consequences; ADR-007 cites the `SHARED_APPS` entries it caused | — |
| D5-E-2 | Per-app README: one `apps/<app>/README.md` for all 19 apps + `apps/billing` — domain, models with one-line purpose, key flows (service entry points), permission resources. 30–60 lines each; derive from code, do not aspirationally document unbuilt features | `apps/*/README.md` | Every model named in a README exists in that app's `models.py`; spot-checked by Lane F | — |
| D5-E-3 | Runbooks: `tenant-provisioning.md` (scripts/create_tenant.py path, slug rules, verification curl), `eskiz.md` [OWNER:O-1], `click.md` [OWNER:O-3], `payme.md` [OWNER:O-4], `uzum.md` [OWNER:O-6], `soliq.md` [OWNER:O-5] — each integration runbook = exact env/`ProviderConfig` fields to set, the `*_USE_MOCK` flag to flip, and a verification step proving real mode works; `backup-restore.md` (per-tenant `pg_dump --schema=<slug>` / restore drill); `onboarding.md` (new backend dev: setup → seed → run tests → read order) | `docs/runbooks/*.md` | Each flip runbook names the exact settings key(s) and mock class being replaced (e.g. `ESKIZ_USE_MOCK`, `MockSoliqClient`); backup runbook tested once against the dev stack with output pasted | — |
| D5-E-4 | Production compose: `docker/docker-compose.prod.yml` — gunicorn web (`--workers $(2*CPU+1)` documented, no `--reload`, no source volume mounts), daphne, `celery-worker-default/io/ai` (queue split from D5-C-8), celery-beat, healthchecks on every service, `restart: unless-stopped`, env via `.env.production` (gitignored, template committed as `.env.production.example`), `DJANGO_SETTINGS_MODULE=config.settings.production` | `docker/docker-compose.prod.yml`, `.env.production.example` | `docker compose -f docker/docker-compose.prod.yml config` validates; no dev secrets or `ESKIZ_USE_MOCK=True` defaults inside; web container passes `manage.py check --deploy` with example env | D5-C-8 merged |
| D5-E-5 | Deploy checklist: ordered list mapping each step to its gate — DNS wildcard + TLS [OWNER:O-8], host + managed Postgres/Redis/S3 [OWNER:O-9], Sentry DSN [OWNER:O-10], `FIELD_ENCRYPTION_KEY` generation [OWNER:O-11], provider credentials flips (reference D5-E-3 runbooks), first `migrate_schemas`, superuser, smoke (healthz, resolve, OTP) | `docs/deploy-checklist.md` | Every [OWNER:O-x] gate that blocks production is listed with its gate ID; each step has a verification command | D5-E-3, D5-E-4 |
| D5-E-6 | `CHANGELOG.md` v1.0.0: keep-a-changelog format; one Added bullet per TASKS.md section shipped (§0–§26 + billing/control-center/fiscal added-scope), Known limitations section (deferred items: chat, video transcoding, A/B prompts, plagiarism integration, WhatsApp OTP) | `CHANGELOG.md` | Every §-section marked done in TASKS.md appears; deferred items match `[ ]` leftovers in TASKS.md | — |

**Models to create / Endpoints to expose / Signals/Celery:** none — documentation and compose only.

**Tests required:** none in pytest. D5-E-4 acceptance is mechanical: `docker compose -f docker/docker-compose.prod.yml config` exits 0 and `manage.py check --deploy` passes with `.env.production.example` values (placeholder secrets allowed, structure must validate).

**Publish to WORKLOG:**
- Paths of every doc produced (ADRs, READMEs, runbooks, deploy checklist, CHANGELOG).
- Code-vs-docs mismatches found while writing READMEs — file to the owning lane same-day; do NOT silently document a bug as intended behavior.
- Which [OWNER:O-x] gates remain open at EOD, copied into the deploy checklist status column.

---

## Lane F — Final E2E + release QA

**Objective.** Execute ROADMAP §7 end-state acceptance items 1–12 EXACTLY, scripted where possible; file every failure as a same-day fix task to the owning lane; produce the release-readiness matrix; tag `v1.0.0`. TDs: all — this lane verifies them.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D5-F-1 | `scripts/e2e_demo.py`: drives the API end-to-end against the running compose stack with httpx + `websockets` (add `websockets` to dev deps — justify in WORKLOG per TD-16). Steps numbered to match §7: (1) provision 2 centers via `apps/tenancy` service or platform API, assert tenant-A JWT → 401 `tenant_mismatch` on tenant B; (2) resolve → OTP request → verify → refresh → logout-everywhere; (3) enroll student (state machine) + link parent + cohort + recurring schedule + assert conflicting rule rejected 422; (4) mark attendance with one absent → assert mock SMS recorded + WS frame received on a live `/ws/notifications/` connection; (5) exam → grades → transcript PDF signed URL 200; (6) assignment + S3 attachment → submit → AI feedback task completes with budget decrement; (7) invoice → mock Payme webhook → allocation + `FiscalReceipt` row → parent sees paid → reconciliation matches; (8) force trial expiry → 402 `subscription_required` → reactivate via control center → 200; (9) print job queued → agent token claim → status to done; (10) trigger scheduled report → S3 object + mock email with link; prints PASS/FAIL per step, exits non-zero on any FAIL | `scripts/e2e_demo.py`, `pyproject.toml` | Fresh `docker compose up` + migrate + seed, then `uv run python scripts/e2e_demo.py` prints 10/10 PASS (steps 11–12 verified separately below); each step idempotent or self-cleaning (unique slugs per run) | All lanes merged (run continuously against master as lanes land) |
| D5-F-2 | Items 11–12: run `uv run pytest --cov=apps --cov=core --cov-fail-under=85`, ruff, mypy, `spectacular --validate`, client compile checks (D5-D-4/5 commands), and `scripts/perf_smoke.py` — record outputs | terminal only | All commands exit 0; outputs captured into the readiness matrix evidence column | D5-B-8, D5-C-5, D5-D-5 |
| D5-F-3 | Bug filing loop: any E2E failure → WORKLOG entry naming the owning lane + a one-line repro; owning lane fixes same-day; re-run the failed step | `agents/WORKLOG.md` | Zero open E2E failures at EOD; every filed bug has a fix commit referenced | D5-F-1 |
| D5-F-4 | Cross-check bookkeeping: TASKS.md — every shipped item `[x]`, every deliberate deferral left `[ ]` and listed in CHANGELOG Known limitations (D5-E-6); review Lane A's pentest doc and Lane E's READMEs for accuracy | `TASKS.md` | No `[~]` items remain except `BLOCKED(O-x)` ones with WORKLOG entries | D5-E-6 |
| D5-F-5 | Release: confirm EOD gate (below) 100% green on master, then `git tag -a v1.0.0 -m "Starforge Edu backend v1.0.0"` + push tag; write the final WORKLOG entry incl. the readiness matrix and handoff pointer to `agents/API-CONTRACT.md` + `clients/` | git tag, `agents/WORKLOG.md` | Tag exists on the green master SHA; final WORKLOG entry appended | D5-F-1..4 |

**Models to create / Endpoints to expose / Signals/Celery:** none.

**Tests required:** the E2E script IS the test — do not duplicate its steps in pytest. Where a step needs server-side state you cannot reach via API (e.g. forcing trial expiry in step 8), use a management command or `schema_context` shell snippet inside the script and document it inline.

### Release-readiness matrix (complete this in the final WORKLOG entry)

| § 7 item | What | Status | Evidence |
|---|---|---|---|
| 1 | Two centers, cross-tenant JWT rejected (`tenant_mismatch`) | ☐ | `e2e_demo.py` step 1 + `tests/test_isolation/` test name |
| 2 | Full OTP flow web + mobile-style (resolve → … → logout-everywhere) | ☐ | `e2e_demo.py` step 2 |
| 3 | Enroll + parent link + cohort + recurring schedule + conflict rejected | ☐ | `e2e_demo.py` step 3 |
| 4 | Absence → mock SMS + in-app over live WS | ☐ | `e2e_demo.py` step 4 |
| 5 | Exam → grades → transcript PDF via signed URL | ☐ | `e2e_demo.py` step 5 |
| 6 | Assignment S3 → submit → AI feedback under budget | ☐ | `e2e_demo.py` step 6 |
| 7 | Invoice → Payme webhook → allocation + fiscal receipt + reconciliation | ☐ | `e2e_demo.py` step 7 |
| 8 | Trial expiry → 402 paywall → reactivate via control center | ☐ | `e2e_demo.py` step 8 |
| 9 | Print job claim flow to done | ☐ | `e2e_demo.py` step 9 |
| 10 | Scheduled report → S3 + mock email link | ☐ | `e2e_demo.py` step 10 |
| 11 | Coverage ≥ 85, ruff/mypy clean, OpenAPI valid, TS+Dart generate | ☐ | D5-F-2 command outputs |
| 12 | Every list endpoint < 150 ms p95, zero N+1 | ☐ | `perf_smoke.py` table + D5-C-1 suite |

**Publish to WORKLOG:** the completed matrix; the tag SHA; the deferred-items list.

---

## Cross-lane integration points (Day 5)

| Producer | Consumer | Interface |
|---|---|---|
| A (D5-A-8 throttle scopes) | D (API-CONTRACT throttle docs), F (E2E expects 429 envelopes) | scope names + rates in WORKLOG |
| B (D5-B-8 coverage floor 85) | C, D, E, F | merges after B must keep total ≥ 85 — bring tests with your code |
| C (D5-C-3 `--scale`, D5-C-5 perf script) | F (item 12 evidence) | `seed_dev.py --scale 10`, `scripts/perf_smoke.py` exit code |
| C (D5-C-8 queue split) | E (prod compose workers), F (E2E async steps) | queue names `default/io/ai` |
| D (D5-D-3 schema diff guard) | every lane merging after D | any endpoint change ⇒ regenerate `openapi.yaml` + clients or CI fails |
| D (D5-D-8 resolve) | F (E2E step 2) | `GET /api/v1/platform/resolve/?slug=` |
| E (D5-E-4 prod compose) | F (release QA review) | `docker-compose.prod.yml` validates |

Merge order: **A → B → C → D → E → F**. Lanes C–F: rebase on master after B lands (new coverage gate) and after D lands (schema guard). Migration-number conflicts (axes from A, indexes from C): later merger renumbers or `makemigrations --merge` per ROADMAP §2.3.

---

## EOD gate — Day 5 closes (and v1.0.0 ships) only when ALL are green

- [ ] `uv run ruff check . && uv run ruff format --check .` — clean
- [ ] `uv run mypy apps core infrastructure config` — clean
- [ ] `uv run pytest -q --cov=apps --cov=core --cov-fail-under=85` — green, floor **85** enforced in CI (TD-20)
- [ ] CI on master: lint, typecheck, test, schema (+ diff guard), audit, clients — all jobs green
- [ ] `openapi.yaml` committed, `spectacular --validate` zero warnings; `clients/typescript` `npm run check` and `clients/dart` `dart analyze` exit 0
- [ ] Fresh stack demo: `docker compose up` → `migrate_schemas` → `seed_dev.py` → `uv run python scripts/e2e_demo.py` → **10/10 PASS**
- [ ] `seed_dev.py --scale 10` then `scripts/perf_smoke.py` exits 0 (every p95 < 150 ms)
- [ ] Security: axes lockout test green, CSP + nosniff + X-Frame headers asserted, prod CORS assertion in place, encryption-at-rest tests green, `pip-audit` job green or risks documented
- [ ] Docs: ADR-001..007, per-app READMEs, all runbooks, deploy checklist, `CHANGELOG.md` v1.0.0, pentest scope — present and reviewed by Lane F
- [ ] TASKS.md: §25, §26, §27 (client artifacts), §29 (compose-prod/runbook scope), §30 ticked; leftovers documented as Known limitations
- [ ] WORKLOG: six lane entries + Lane F final entry with the completed release-readiness matrix
- [ ] `git tag v1.0.0` pushed on the green master SHA; `agents/API-CONTRACT.md` + `clients/` handed to frontend
