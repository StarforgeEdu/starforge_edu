# Architecture

## Tenancy
- **Strategy:** schema-per-tenant via `django-tenants`.
- **Tenant model:** `apps.tenancy.Center` (lives in `SHARED_APPS` only).
- **Hostname → tenant:** `apps.tenancy.Domain` rows; e.g. `acme.starforge.uz` → `Center(schema_name='acme')`.
- **`SHARED_APPS`:** `django_tenants`, `apps.tenancy`, Django contrib (admin/auth/contenttypes/sessions/messages/staticfiles), `django_celery_beat`, `channels`, `corsheaders`.
- **`TENANT_APPS`:** users, auth, org (Branch+Department), the 16 domain apps, plus contrib (so Django admin works inside a tenant).
- **Migrations:** `migrate_schemas --shared` for public; `migrate_schemas` runs per tenant when a new Center is created (auto, via `auto_create_schema=True`).
- **Celery:** `tenant-schemas-celery` activates the right schema for every task. Pass `_schema_name="acme"` when delaying from a context that already knows the tenant (otherwise the request middleware already set the connection).
- **Channels:** `TenantAwareAuthMiddleware` validates Host and browser Origin, rate-limits the handshake, resolves an active tenant, then authenticates the opaque session key. Every environment rejects query-string tokens; clients use the HttpOnly cookie or a credential-bearing `Sec-WebSocket-Protocol` value that is scrubbed and never echoed. Consumers retain only a non-secret session row ID and reauthorize before each event. **Never** access tenant data before this middleware has run.
- **Management commands:** wrap with `schema_context("acme"):` or use `tenant_command`.

## Auth
- **Credentials:** an opaque random `users.Session.key` is the Bearer credential. Sessions expire after `SESSION_TTL_DAYS`, are tenant-bound by the schema containing the row, and are revoked server-side. Roles are loaded live, so grants and revocations do not wait for token expiry. Role-native sessions load only memberships whose account kind matches the authenticated student, teacher, parent, or staff principal; the internal bridge User cannot combine grants across profiles.
- **Role login:** `POST /api/v1/auth/role-login/ {username, password}` authenticates the StudentProfile, TeacherProfile, ParentProfile, or StaffProfile table and returns `{access, role, must_change_password}`. Each profile owns its identity and password.
- **Platform login:** `POST /api/v1/auth/login/` exists only on the bare public/apex
  host and authenticates a real staff/superuser Django User. Tenant URLConfs do not
  expose it; role compatibility principals have unusable passwords and are rejected.
- **Password reset:** `POST /api/v1/auth/password/reset/request/ {identifier, account_type}`
  (always 202, anti-enumeration) → SMS/email OTP →
  `POST /api/v1/auth/password/reset/confirm/ {identifier, account_type, code, new_password}`
  (ends all sessions). `account_type` is one of `student`, `teacher`, `parent`, or
  `staff`; it prevents a shared contact value from selecting the wrong role account.
- **Password change:** ends all existing sessions and returns one fresh opaque session.
- **Admin:** `/admin/` uses Django sessions for platform operators. Role accounts are edited in role-specific admin sections and never selected from the User table.
- **Logout:** `POST /api/v1/auth/logout/` revokes only the presented session;
  `POST /api/v1/auth/logout-all/` revokes every session for the current principal.
  There is no client-held refresh token.

## Permissions
- **Matrix:** `core/permissions.py: ROLE_PERMISSION_MATRIX` — single source of truth.
- **Action-level:** layered views authenticate with `@require_auth` and call `check_perm(request, "resource:verb")`. DRF reports views retain `RolePermission`. Missing permission grants fail closed.
- **Row-level:** `read_self` / `read_own_children` verbs are enforced by queryset scoping in `selectors.py` (the gate grants `:read`; the selector narrows rows to self / linked children).
- **Object-level:** selectors/repositories apply exact `RoleMembership(user, branch[, department])` scope before lookup; out-of-scope IDs resolve as 404 where existence must not leak.
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
- ASGI via bounded Daphne handshakes/frame sizes/lifetimes; the Redis channel layer encrypts payloads and uses short, heartbeat-refreshed group expiry.
- `/ws/ping/`, the exact-principal notification hint stream, scoped cohort-attendance stream, and recoverable exact-participant messaging-thread pointer stream aggregate through `infrastructure/websocket/routing.py`. Query-string credentials are rejected in every environment. REST remains the content authority after every reconnect.

## Periodic tasks, DLQ & metrics (D4-LF)
- **One schedule:** `settings.CELERY_BEAT_SCHEDULE` is the single source of truth; `DatabaseScheduler` ingests it at beat startup. No app defines an ad-hoc periodic schedule (enforced by `tests/test_beat_consolidation.py`). The consolidated Day-1..4 table:

  | Beat key | Task | Schedule | Scope |
  |---|---|---|---|
  | `runtime-heartbeat` | `celery_tasks.health_tasks.record_runtime_heartbeat` | every 30 sec | runtime |
  | `quarantine-stale-print-leases` | `celery_tasks.print_tasks.quarantine_stale_print_leases` | every 60 sec | tenant fan-out / bounded manual-review quarantine |
  | `purge-expired-otps` | `celery_tasks.cleanup_tasks.purge_expired_otps` | daily | tenant fan-out |
  | `mark-absent-after-lesson` | `celery_tasks.attendance_tasks.mark_absent_after_lesson` | every 15 min | per-tenant |
  | `send-lesson-reminders` | `celery_tasks.schedule_tasks.send_lesson_reminders` | every 5 min | per-tenant |
  | `archive-completed-terms` | `celery_tasks.schedule_tasks.archive_completed_terms` | weekly | per-tenant |
  | `send-due-soon-reminders` | `celery_tasks.assignment_tasks.send_due_soon_reminders` | hourly | tenant fan-out |
  | `late-payment-reminders` | `celery_tasks.finance_tasks.late_payment_reminders` | daily | tenant fan-out |
  | `cleanup-old-audit-logs` | `celery_tasks.audit_tasks.cleanup_old_audit_logs` | weekly | public + tenant fan-out |
  | `run-nightly-metering` | `celery_tasks.billing_tasks.run_nightly_metering` | daily | public |
  | `deactivate-expired-trials` | `celery_tasks.tenancy_tasks.deactivate_expired_trials` | hourly | public |
  | `run-due-report-schedules` | `celery_tasks.report_tasks.run_due_report_schedules` | hourly at :00 | tenant fan-out |
  | `dispatch-scheduled-campaigns` | `celery_tasks.campaign_tasks.dispatch_scheduled_campaigns` | every 5 min | tenant fan-out |
  | `prune-webhook-events` | `celery_tasks.payment_tasks.prune_webhook_events` | daily | tenant fan-out |
  | `reconcile-fiscal-receipts` | `celery_tasks.payment_tasks.reconcile_fiscal_receipts` | every 5 min | tenant fan-out / critical queue |
- **Registration:** every task module is imported by `celery_tasks/tasks.py` (the autodiscovery aggregator `app.autodiscover_tasks(["celery_tasks"])` imports). A beat entry pointing at an unregistered task is a hard test failure (the Day-1 blocker class).
- **DLQ:** `celery_tasks/observability.py` records bounded, scrubbed failure metadata (`task`, task id, argument types, kwarg names, exception class/detail, schema, timestamp) in `starforge:dlq`; raw task payloads are never stored.
  - **Inspect:** `redis-cli LLEN starforge:dlq` then `redis-cli LRANGE starforge:dlq 0 -1`.
  - **Drain:** inspect or pop an entry, then replay from the originating system of record after reviewing the failure; the DLQ intentionally does not retain raw arguments.
- **Duration metrics:** `task_prerun`/`task_postrun` handlers log `task=… state=… duration_ms=…` on the `starforge.celery` logger (tenant-tagged via `TenantSchemaFilter`).
- **Wiring:** `config/celery.py` calls `connect_celery_observability(app)` once after building the app (idempotent via `dispatch_uid`).

## i18n (D4-LF)
- **Languages:** `uz` (primary / `LANGUAGE_CODE`), `en`, `ru`. `LocaleMiddleware` sits after `SessionMiddleware`, before `CommonMiddleware`; `Accept-Language` selects the active language for API error messages.
- **Catalogs:** `locale/<lang>/LC_MESSAGES/django.{po,mo}`. On a Linux runner `manage.py makemessages -a` + `compilemessages` are authoritative (CI runs `compilemessages`); on the Windows dev box (no GNU gettext) `scripts/build_locale.py` writes the `.po` and compiles the `.mo` with a pure-Python MO writer.
- **Error-path discipline:** every user-facing string raised from a service/serializer error path is `gettext_lazy`-wrapped. `scripts/check_i18n.py` is the CI gate (zero bare literals).
- **Notification templates:** `notifications.render_template` picks the variant by the recipient's `User.preferred_language`, falling back center-default → en → uz and logging a warning on a missing variant. Every event type carries uz+en+ru in-app rows (seeded in `notifications/0003`).
- **Profile:** `PATCH /api/v1/users/me/ {preferred_language}` is the self-service setter.

## Separate deliverables
- Branch print agent (separate Go/Python repository).
- Frontends (React + Flutter).
- Real provider credentials and off-site backup credentials are deployment secrets, not committed source.
