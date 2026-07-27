# Architecture

## Tenancy
- **Strategy:** schema-per-tenant via `django-tenants`.
- **Tenant model:** `apps.tenancy.Center` (lives in `SHARED_APPS` only).
- **Hostname → tenant:** `apps.tenancy.Domain` rows; e.g. `acme.starforge.uz` → `Center(schema_name='acme')`.
- **`SHARED_APPS`:** `django_tenants`, `apps.tenancy`, Django contrib (admin/auth/contenttypes/sessions/messages/staticfiles), `django_celery_beat`, `channels`, `corsheaders`.
- **`TENANT_APPS`:** users, auth, org (Branch+Department), the 16 domain apps, plus contrib (so Django admin works inside a tenant).
- **Migrations:** `migrate_schemas --shared` for public; `migrate_schemas` runs per tenant when a new Center is created (auto, via `auto_create_schema=True`).
- **Celery:** `tenant-schemas-celery` activates the right schema for every task. Pass `_schema_name="acme"` when delaying from a context that already knows the tenant (otherwise the request middleware already set the connection).
- **Channels:** `TenantAwareJWTAuthMiddleware` resolves tenant from hostname, then authenticates the user. **Never** access tenant data from a consumer before this middleware has run.
- **Management commands:** wrap with `schema_context("acme"):` or use `tenant_command`.

## Auth
- **Tokens:** JWT (simplejwt). Access 15min, refresh 14d, rotation on, blacklist on. Both tokens carry TD-1 claims: `schema` (issuing tenant — enforced on access AND refresh paths, 401 `tenant_mismatch` otherwise) and `tv` (token version — bumped on password change, role change, logout-everywhere).
- **Login flow:** `POST /api/v1/auth/login/ {username, password}` → `{access, refresh}` (owner decision 2026-06-11; supersedes OTP-as-login).
- **Password reset:** `POST /api/v1/auth/password/reset/request/ {identifier}` (always 202, anti-enumeration) → SMS/email OTP → `POST /api/v1/auth/password/reset/confirm/ {identifier, code, new_password}` (ends all sessions).
- **Password change:** `POST /api/v1/auth/password/change/` — ends all other sessions, returns a fresh pair.
- **Admin:** `/admin/` sessions; staff log in with username (stock backend) or phone/email (`PhoneOrEmailBackend`).
- **Logout:** `POST /api/v1/auth/logout/ {refresh}` blacklists one refresh; `POST /api/v1/auth/logout-all/` revokes everything.

## Permissions
- **Matrix:** `core/permissions.py: ROLE_PERMISSION_MATRIX` — single source of truth.
- **Action-level (TD-5):** viewsets declare `resource = "<name>"` (verbs derived per action: list/retrieve → `:read`, create/update/destroy → `:write`) plus `required_perms: dict` for custom actions/overrides. Views with neither mapping are **fail-closed** (denied). The flat `required_perm` attribute is gone.
- **Row-level:** `read_self` / `read_own_children` verbs are enforced by queryset scoping in `selectors.py` (the gate grants `:read`; the selector narrows rows to self / linked children).
- **Object-level:** `ObjectScopedPermission` reads `view.object_scope = "branch" | "department"` and checks `RoleMembership(user, branch[, department])`.
- **Director / superuser:** bypass.

## Events / cross-app coupling
- Apps emit Django signals; `apps/notifications/services.dispatch(event)` is the canonical fan-out for sms/email/push/in-app. Apps must NOT call channel adapters directly.
- Audit logging is signal-driven and lives in `apps/audit/` (out of `apps/reports/`).

## Cost guardrails
- AI calls (`apps/ai/`) are Celery-only. `TenantAIBudget` checked before queueing.
- Anthropic client (`infrastructure/ai/anthropic_client.py`) caches identical prompt+system+model triplets in Redis; Anthropic prompt caching is enabled by default at the request level.
- OTP is throttled three ways (per-phone, per-IP, global). Eskiz mocked in dev.

## Storage
- **`STORAGES["default"]`** is S3-compatible. Use the same code against AWS S3 (prod) and MinIO (dev).
- Signed up/download via `infrastructure/storage/s3_client.py`.

## Realtime
- ASGI via Daphne; channel layer on Redis (`channels-redis`).
- One demo consumer at `/ws/ping/` proves the wiring; per-app routing aggregates into `infrastructure/websocket/routing.py`.

## Periodic tasks, DLQ & metrics (D4-LF)
- **One schedule:** `settings.CELERY_BEAT_SCHEDULE` is the single source of truth; `DatabaseScheduler` ingests it at beat startup. No app defines an ad-hoc periodic schedule (enforced by `tests/test_beat_consolidation.py`). The consolidated Day-1..4 table:

  | Beat key | Task | Schedule | Scope |
  |---|---|---|---|
  | `purge-expired-otps` | `celery_tasks.cleanup_tasks.purge_expired_otps` | daily 03:00 | per-tenant iterate |
  | `flush-expired-jwt-blacklist` | `celery_tasks.cleanup_tasks.flush_expired_jwt_blacklist` | weekly Sun 04:30 | per-tenant iterate |
  | `mark-absent-after-lesson` | `celery_tasks.attendance_tasks.mark_absent_after_lesson` | every 15 min | per-tenant |
  | `send-lesson-reminders` | `celery_tasks.schedule_tasks.send_lesson_reminders` | every 5 min | per-tenant |
  | `archive-completed-terms` | `celery_tasks.schedule_tasks.archive_completed_terms` | weekly | per-tenant |
  | `send-due-soon-reminders` | `celery_tasks.assignment_tasks.send_due_soon_reminders` | daily 17:00 | per-tenant |
  | `late-payment-reminders` | `celery_tasks.finance_tasks.late_payment_reminders` | daily 09:00 | per-tenant |
  | `cleanup-old-audit-logs` | `celery_tasks.audit_tasks.cleanup_old_audit_logs` | weekly Sun 04:00 | per-tenant |
  | `run-nightly-metering` | `celery_tasks.billing_tasks.run_nightly_metering` | daily 01:00 | public |
  | `deactivate-expired-trials` | `celery_tasks.tenancy_tasks.deactivate_expired_trials` | daily 00:30 | public |
  | `run-due-report-schedules` | `celery_tasks.report_tasks.run_due_report_schedules` | hourly | per-tenant |
  | `nightly-platform-aggregation` | `celery_tasks.report_tasks.nightly_platform_aggregation` | daily 02:00 | public |

  (The DAY-4 plan's conceptual names `meter_usage_and_flip_states` / `expire_trials` / `assignment_due_soon` map to the **actually built** task names `run_nightly_metering` / `deactivate_expired_trials` / `send_due_soon_reminders` — the code is the source of truth.)
- **Registration:** every task module is imported by `celery_tasks/tasks.py` (the autodiscovery aggregator `app.autodiscover_tasks(["celery_tasks"])` imports). A beat entry pointing at an unregistered task is a hard test failure (the Day-1 blocker class).
- **DLQ (TASKS §22):** `celery_tasks/observability.py` connects a `task_failure` handler (fires only after retries are exhausted) that LPUSHes `{task, args, kwargs, exc, schema, ts}` to the Redis list `starforge:dlq`. The push is best-effort (a Redis hiccup never compounds the task failure).
  - **Inspect:** `redis-cli LLEN starforge:dlq` then `redis-cli LRANGE starforge:dlq 0 -1`.
  - **Drain / replay:** pop with `redis-cli RPOP starforge:dlq`, read the `task` + `args`/`kwargs` + `schema`, then re-enqueue via `manage.py shell`: `from celery_tasks.<module> import <task>; <task>.delay(*args, _schema_name=schema, **kwargs)`. Purge a poison entry by RPOP-ing it without replay.
- **Duration metrics:** `task_prerun`/`task_postrun` handlers log `task=… state=… duration_ms=…` on the `starforge.celery` logger (tenant-tagged via `TenantSchemaFilter`).
- **Wiring:** `config/celery.py` calls `connect_celery_observability(app)` once after building the app (idempotent via `dispatch_uid`).

## i18n (D4-LF)
- **Languages:** `uz` (primary / `LANGUAGE_CODE`), `en`, `ru`. `LocaleMiddleware` sits after `SessionMiddleware`, before `CommonMiddleware`; `Accept-Language` selects the active language for API error messages.
- **Catalogs:** `locale/<lang>/LC_MESSAGES/django.{po,mo}`. On a Linux runner `manage.py makemessages -a` + `compilemessages` are authoritative (CI runs `compilemessages`); on the Windows dev box (no GNU gettext) `scripts/build_locale.py` writes the `.po` and compiles the `.mo` with a pure-Python MO writer.
- **Error-path discipline:** every user-facing string raised from a service/serializer error path is `gettext_lazy`-wrapped. `scripts/check_i18n.py` is the CI gate (zero bare literals).
- **Notification templates:** `notifications.render_template` picks the variant by the recipient's `User.preferred_language`, falling back center-default → en → uz and logging a warning on a missing variant. Every event type carries uz+en+ru in-app rows (seeded in `notifications/0003`).
- **Profile:** `PATCH /api/v1/users/me/ {preferred_language}` is the self-service setter.

## Out of scope (post-v1)
- Branch print agent (separate Go/Python repo).
- Live integration with Click / Payme / Uzum (stubs only in v1).
- Frontends (React + Flutter).
- Production deploy beyond the compose stack.
