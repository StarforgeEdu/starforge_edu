# DAY 4 — Intelligence, realtime & control center

At start of day the platform moves data and money: people/org (Day 1), the academic engine + content/storage (Day 2), finance/payments/notifications/audit/billing-paywall (Day 3) are live, tested, and on `master`. Day 4 makes the platform *smart and observable*: budgeted AI features running Celery-only, a report library (PDF/Excel, scheduled, S3+signed URL), live WebSocket consumers fed exclusively by `notifications.dispatch()`, the printing pipeline, the platform control center on the public schema, an i18n pass, and one consolidated beat schedule. By EOD every TD listed below is implemented, coverage ≥ 80%, and the Day-4 demo script passes end to end.

Merge order today: **A → B → C → D → E → F** (ROADMAP §2.3). Lane F merges last because it touches every app.

---

## Prerequisites (verify before writing code)

Read the last 2 days of `agents/WORKLOG.md` first — exact signal names, service signatures, and any deviations from DAY-2/DAY-3 plans are recorded there. Then verify on `master`:

- [ ] `uv run pytest -q` green; `uv run pytest --cov=apps --cov=core --cov-fail-under=80 -q` passes (TD-20 floor after Day 3).
- [ ] Day-3 Lane C landed: `uv run python manage.py shell -c "from apps.notifications.services import dispatch"` imports (TD-15 producer).
- [ ] Day-3 Lane E landed: `uv run python manage.py shell -c "from apps.billing.models import Plan, Subscription, UsageSnapshot"` imports (TD-8).
- [ ] Day-2 Lane D landed: `apps/assignments/signals.py` exposes a submission-created signal (name per WORKLOG; expected `submission_created`).
- [ ] Day-2 Lane C landed: academics `Exam`/`Transcript` + transcript PDF Celery task (TD-14, weasyprint already in `pyproject.toml` — if not, Lane B adds it).
- [ ] Day-2 Lane E landed: content `LessonFile` with `size_bytes` and per-tenant S3 prefix `{schema_name}/...` (TASKS §23).
- [ ] Day-1 Lane A's TD-17 fixes present: `infrastructure/ai/anthropic_client.py::_cache_key` includes `max_tokens` + `effort` (Lane A re-verifies with a regression test today).
- [ ] `core/authentication.py` TD-1 class active (schema + tv claims enforced) — Lanes C and E extend it.

If any prerequisite is missing, fixing it is your first task — log `[out-of-lane]` in WORKLOG.

---

## Lane A — AI (apps/ai) [OWNER:O-2]

**Objective:** Replace the `AiItem` placeholder with the budgeted AI subsystem: `TenantAIBudget` / `AIRequest` / `AIPrompt`, pre-flight budget checks, Celery-only execution through `infrastructure/ai/anthropic_client.py::complete()`, PII redaction before every prompt, and the three v1 features wired to Day-2 signals. Implements TASKS §18; TD-2 (mock-first), TD-13 (no magic numbers), TD-17 (cache-key verify). Publishes the `ai-tokens-consumed` metering interface for billing.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D4-LA-1 | Models + migration: `TenantAIBudget`, `AIRequest`, `AIPrompt`; delete `AiItem` + its CRUD | `apps/ai/models.py`, `apps/ai/migrations/`, `apps/ai/admin.py` | Migration applies via `migrate_schemas`; constraints below exist; `AiItemViewSet` gone from `apps/ai/views.py` + `urls.py` | — |
| D4-LA-2 | Anthropic mock (TD-2): `ANTHROPIC_USE_MOCK` setting (default True outside production) — `complete()` returns deterministic text + fake usage when on | `infrastructure/ai/anthropic_client.py`, `config/settings/base.py`, `config/settings/production.py` | With mock on, `complete()` makes zero HTTP calls; same inputs → same `{text, usage}`; flag documented in `agents/OWNER-ACTIONS.md` gate O-2 | — |
| D4-LA-3 | TD-17 regression test: Redis cache key includes `max_tokens` + `effort` | `apps/ai/tests/test_anthropic_client.py` | Two `complete()` calls differing only in `effort` (or `max_tokens`) produce different `_cache_key` values; fix `_cache_key` if Day 1 missed it | D4-LA-2 |
| D4-LA-4 | Budget service: `check_and_reserve_budget(*, feature, estimated_tokens, requested_by, source_app, source_id) -> AIRequest`; `record_usage(ai_request_id, usage) -> None` (F-expression counters, `select_for_update` on budget row, day/month anchors roll over) | `apps/ai/services.py` | Over-budget call raises `StarforgeError` code `ai_budget_exceeded` and creates `AIRequest(status="denied_budget")`; usage recording is atomic; anchors reset counters on date change | D4-LA-1 |
| D4-LA-5 | PII redaction: `redact(text, *, known_names: list[str]) -> tuple[str, dict]` + `restore(text, mapping)` — regex for E.164 phones, national-ID patterns (2 letters + 7 digits), emails, plus exact-match replacement of involved student/parent names with `[STUDENT_1]`-style tokens | `apps/ai/redaction.py`, tests | Round-trip test: redact→restore is lossless; a prompt containing `+998901234567` and a known name contains neither after redaction; mapping persisted on `AIRequest.redaction_map` (encrypted, TD-11) | D4-LA-1 |
| D4-LA-6 | Celery tasks (new module): `run_assignment_feedback(submission_id)`, `run_exam_generation(ai_request_id)`, `run_content_summary(lesson_file_id)` — each: load `AIPrompt` active version → redact → `complete()` → restore → persist output → `record_usage` | `celery_tasks/ai_tasks.py`, `celery_tasks/__init__.py` (import the module so autodiscovery registers it) | Tasks visible in `app.tasks`; re-delivery of the same source row is a no-op (idempotency key); failure sets `AIRequest.status="failed"` + `error_detail`; max_retries=3 exponential | D4-LA-4, D4-LA-5 |
| D4-LA-7 | Wire features to Day-2 signals: assignment feedback on submission-created (per WORKLOG name); content summary on file-upload-confirmed; exam generation is request-driven (endpoint below), gated by `CenterSettings.ai_exam_generation_enabled` (TD-13, default False) | `apps/ai/receivers.py`, `apps/ai/apps.py` (`ready()` import) | Creating a Submission in a test enqueues `run_assignment_feedback` exactly once; with budget exhausted nothing is enqueued and a `denied_budget` row exists | D4-LA-6 |
| D4-LA-8 | Endpoints + perms (TD-5): viewsets below; add `ai:read`/`ai:write` matrix entries (teacher, head_of_dept get both; student/parent none) | `apps/ai/views.py`, `serializers.py`, `selectors.py`, `urls.py`, `core/permissions.py` (additive) | Per-action `required_perms`; teacher 202 on exam-gen, student 403; OpenAPI examples present | D4-LA-4 |
| D4-LA-9 | Usage selector for billing: `apps.ai.selectors.tokens_consumed(start: date, end: date) -> int` (runs inside the active tenant schema; sums `AIRequest` input+output tokens) | `apps/ai/selectors.py` | Returns correct sum in a test with 3 AIRequests; documented in WORKLOG as the **ai-tokens-consumed** interface | D4-LA-1 |

**Models (concrete schema):**
- `TenantAIBudget` — `daily_token_limit: PositiveIntegerField (default=settings.AI_DEFAULT_DAILY_TOKENS)`, `monthly_token_limit: PositiveIntegerField (default=settings.AI_DEFAULT_MONTHLY_TOKENS)`, `tokens_used_today: PositiveBigIntegerField default 0`, `tokens_used_month: PositiveBigIntegerField default 0`, `day_anchor: DateField`, `month_anchor: DateField`, `is_enabled: BooleanField default True`, `updated_at: auto_now`. Singleton per tenant (enforce `pk=1` pattern or `UniqueConstraint` on a constant field).
- `AIRequest` — `feature: CharField(32, choices: assignment_feedback|exam_generation|content_summary, db_index)`, `status: CharField(16, choices: queued|running|succeeded|failed|denied_budget, db_index)`, `prompt: FK(AIPrompt, PROTECT)`, `requested_by: FK(users.User, SET_NULL, null)`, `source_app: CharField(32)`, `source_id: PositiveBigIntegerField`, `idempotency_key: CharField(128, unique)` (= `f"{feature}:{source_app}:{source_id}:v{prompt.version}"`), `input_tokens/output_tokens/cache_read_tokens/cache_creation_tokens: PositiveIntegerField default 0`, `cost_microusd: BigIntegerField default 0`, `redaction_map: EncryptedTextField (core/fields.py, TD-11)`, `output_text: TextField blank`, `error_detail: TextField blank`, `celery_task_id: CharField(64, blank)`, `created_at/started_at/finished_at`. `Meta.ordering = ("-created_at",)`.
- `AIPrompt` — `feature: CharField(32, choices as above)`, `version: PositiveSmallIntegerField`, `system_prompt: TextField`, `user_template: TextField`, `max_output_tokens: PositiveIntegerField`, `effort: CharField(16, default "medium")`, `token_cost_cap: PositiveIntegerField` (per-feature cap, TASKS §18), `is_active: BooleanField`. Constraints: `unique_together(feature, version)`; partial `UniqueConstraint(feature, condition=is_active=True)`. Seed one active prompt per feature in a data migration.

**Endpoints to expose:**
- `GET /api/v1/ai/requests/` — `ai:read` — paginated `AIRequest` log (filters: feature, status, date range); response: id, feature, status, tokens, cost, created_at.
- `GET /api/v1/ai/budget/` — `ai:read` — `{daily_token_limit, monthly_token_limit, tokens_used_today, tokens_used_month, is_enabled}`.
- `PATCH /api/v1/ai/budget/` — `ai:manage` (director-only via `*:*`) — update limits / `is_enabled`.
- `POST /api/v1/ai/exam-generation/` — `ai:write` — body `{subject_id, exam_type, question_count, difficulty}` → 202 `{request_id}`; 403 `feature_disabled` when the CenterSettings gate is off; 429-style `ai_budget_exceeded` envelope when over budget.
- `GET /api/v1/ai/usage-report/?month=YYYY-MM` — `ai:read` — totals per feature `{feature, requests, input_tokens, output_tokens, cost_microusd}`.

**Settings (additive to `config/settings/base.py`):** `ANTHROPIC_USE_MOCK`, `AI_COST_PER_MTOK_INPUT_MICROUSD`, `AI_COST_PER_MTOK_OUTPUT_MICROUSD` (placeholder prices; real pricing [OWNER:O-2]).

**Signals/Celery + idempotency:** receivers enqueue with `transaction.on_commit`; the `AIRequest.idempotency_key` unique constraint makes duplicate signal deliveries no-ops (use `get_or_create`, skip enqueue on existing). Tasks: `max_retries=3`, exponential backoff, `acks_late=True`; budget is reserved pre-enqueue and reconciled with real usage post-completion (never double-counted on retry — guard on `status`).

**Tests required (agents/TESTING.md matrix):**
- [ ] budget exhaustion → `denied_budget` row + `ai_budget_exceeded` envelope, nothing enqueued
- [ ] duplicate signal delivery → single `AIRequest` (idempotency)
- [ ] redaction round-trip lossless; phone/national-id/name absent from prompt sent to `complete()`
- [ ] cross-tenant isolation on `/ai/requests/` (tenant-A token sees zero tenant-B rows)
- [ ] permission-denied per role (student/parent 403 on every AI endpoint)
- [ ] query-count assertion on `GET /ai/requests/`
- [ ] TD-17 cache-key regression (effort + max_tokens vary the Redis key)
- [ ] Celery task runs under the scheduling tenant's schema (TASKS §26)

**Publish to WORKLOG:** `apps.ai.selectors.tokens_consumed(start, end)` (consumed by Lane B aggregation + billing metering); signal-wiring confirmation (which Day-2 signal names were used); `ANTHROPIC_USE_MOCK` flag.

---

## Lane B — Reports (apps/reports)

**Objective:** Replace `ReportItem` with `Report`/`ReportRun`/`ReportSchedule`, a six-generator library (each a pure selector + renderer), Celery one-shot generation → S3 → signed URL delivered via `notifications.dispatch`, hourly schedule scan, per-role visibility, and the nightly cross-tenant aggregation that fills `apps.billing.UsageSnapshot`. Implements TASKS §20; TD-14 (weasyprint/openpyxl, Celery→S3→signed URL), TD-16 (deps), TD-8 (feeds UsageSnapshot — coordinate with Lane E).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D4-LB-1 | Models + migration; drop `ReportItem`; data migration seeding the 6 library rows | `apps/reports/models.py`, `migrations/` | `migrate_schemas` clean; 6 `Report` rows exist per tenant after migrate | — |
| D4-LB-2 | Deps: add `weasyprint`, `openpyxl` to `pyproject.toml` if Day 2 didn't (TD-16); justify in WORKLOG | `pyproject.toml` | `uv sync` clean; imports resolve | — |
| D4-LB-3 | Generator library: `base.py` protocol (`collect(params) -> dict` pure selector with `select_related`/`prefetch_related`; `render_pdf(data) -> bytes`; `render_xlsx(data) -> bytes`) + `enrollment.py`, `attendance.py`, `grades.py`, `finance.py`, `ai_usage.py`, `storage_usage.py` | `apps/reports/generators/*.py`, `templates/documents/reports/*.html` (uz/ru/en variants, TD-14) | Each generator unit-tested against factory data; `ai_usage` calls `apps.ai.selectors.tokens_consumed`; `storage_usage` sums `LessonFile.size_bytes`; zero N+1 (query-count test per collect) | D4-LA-9 (interface only — code against WORKLOG announcement) |
| D4-LB-4 | One-shot run: implement `celery_tasks/report_tasks.py::build_report(run_id)` for real — render → upload `{schema_name}/reports/{run_id}.{pdf,xlsx}` via `infrastructure/storage/s3_client.py` → `presign_download` → `notifications.dispatch(event="report.ready", ...)` (never call email client directly — docs/adding-an-app.md) | `celery_tasks/report_tasks.py`, `apps/reports/services.py` | Run row goes queued→running→done with `s3_key` + `file_bytes` set; failure → failed + `error`; task idempotent (done runs are skipped); retries max 3 exponential | D4-LB-3 |
| D4-LB-5 | Endpoints + visibility: directors all reports; accountants `finance` only; teachers `enrollment/attendance/grades` scoped to own cohorts (param scoping enforced in `selectors.py`, not the view) | `apps/reports/views.py`, `serializers.py`, `selectors.py`, `urls.py`, `core/permissions.py` (additive: `reports:write` for teacher/head_of_dept/accountant) | Accountant POST on `grades` report → 403; teacher run of `attendance` only returns own-cohort rows (test proves scoping) | D4-LB-1 |
| D4-LB-6 | Schedules: `ReportSchedule` + hourly beat task `run_due_report_schedules` (iterates tenants, fires `build_report` for due rows, stamps `last_run_at`) — registered in Lane F's consolidated schedule, not ad-hoc | `apps/reports/services.py`, `celery_tasks/report_tasks.py` | A weekly schedule due now creates exactly one run; re-running the scan within the hour creates none (`last_run_at` guard) | D4-LB-4 |
| D4-LB-7 | Cross-tenant nightly aggregation: `nightly_platform_aggregation` (public schema) — for each Center, under `schema_context` collect: student count, DAU (`users.User.last_seen_at >= today`), storage bytes, AI tokens; upsert `apps.billing.UsageSnapshot(center, date, ...)`. If UsageSnapshot lacks a field, add an **additive** migration in `apps/billing` and announce in WORKLOG (coordinate with Lane E, who reads it) | `celery_tasks/report_tasks.py`, `apps/billing/` (additive only) | Two-tenant test: snapshots written for both centers with correct counts; task rerun same day updates, not duplicates (`unique_together(center, date)`) | D4-LA-9 |

**Models:** `Report` — `key: CharField(32, unique, choices: enrollment|attendance|grades|finance|ai_usage|storage_usage)`, `title: CharField(120)`, `description: TextField blank`, `allowed_roles: JSONField` (list of `core.permissions.Role` codes), `default_format: CharField(8, choices pdf|xlsx)`. `ReportRun` — `report: FK(Report, PROTECT)`, `requested_by: FK(users.User, SET_NULL, null)`, `params: JSONField default dict`, `format: CharField(8)`, `status: CharField(16, choices queued|running|done|failed, db_index)`, `s3_key: CharField(512, blank)`, `file_bytes: PositiveBigIntegerField default 0`, `error: TextField blank`, `created_at/started_at/finished_at`. `ReportSchedule` — `report: FK`, `cadence: CharField(16, choices weekly|monthly)`, `weekday: PositiveSmallIntegerField null`, `day_of_month: PositiveSmallIntegerField null`, `hour: PositiveSmallIntegerField default 7`, `format`, `params: JSONField`, `recipient_ids: JSONField`, `is_active: BooleanField default True`, `last_run_at: DateTimeField null`, `created_by: FK SET_NULL`. `CheckConstraint`: weekly⇒weekday set, monthly⇒day_of_month set.

**Endpoints to expose:**
- `GET /api/v1/reports/` — `reports:read` — library list filtered to the caller's `allowed_roles`.
- `POST /api/v1/reports/runs/` — `reports:write` — body `{report_key, format, params}` → 202 `{run_id}`; 403 when the report's `allowed_roles` excludes the caller.
- `GET /api/v1/reports/runs/<id>/` — `reports:read` — `{status, error, download_url}` (`download_url` is a fresh presign, only when `done`).
- `GET|POST|PATCH /api/v1/reports/schedules/` — `reports:write` — manage `ReportSchedule` rows; list filtered by role like the library.

**Signals/Celery + idempotency:** `build_report` skips runs not in `queued` (safe re-delivery); `run_due_report_schedules` uses the `last_run_at` guard; `nightly_platform_aggregation` upserts on `(center, date)` — rerunning any of them is harmless. Delivery goes through `notifications.dispatch("report.ready", ...)`, never `infrastructure/email` directly.

**Tests required (TESTING.md matrix):**
- [ ] generator correctness ×6 against factory data (including `ai_usage` consuming Lane A's selector)
- [ ] role visibility matrix (director/accountant/teacher × 6 report keys)
- [ ] teacher cohort scoping enforced in selector (foreign-cohort rows absent)
- [ ] signed-URL flow against MinIO (or mocked boto3 per TESTING.md)
- [ ] schedule-due exactly-once within the hour
- [ ] two-tenant aggregation writes both snapshots, no bleed
- [ ] cross-tenant isolation on `/reports/runs/`; query-count on list + each `collect()`

**Publish to WORKLOG:** `build_report(run_id)` contract; `nightly_platform_aggregation` + final `UsageSnapshot` field list (Lane E consumes); `report.ready` dispatch event name (Lane C in-app channel carries it).

---

## Lane C — Channels realtime (TASKS §21, TD-15)

**Objective:** Real consumers replace the demo-only state: `NotificationConsumer` (`/ws/notifications/`) and `AttendanceConsumer` (`/ws/cohorts/<id>/attendance/`), groups `user.{id}` / `cohort.{id}` / `branch.{id}` joined from RoleMemberships at connect, 30s heartbeat, disconnect cleanup, and `notifications.dispatch()` as the ONLY group producer. `/ws/ping/` stays as smoke.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D4-LC-1 | Harden WS auth to TD-1: `_user_from_token` must validate `schema` claim against the resolved tenant and `tv` against the user (reuse `core/authentication.py` validation, replacing the bare `UntypedToken` check) | `infrastructure/websocket/middleware.py` | Token minted in tenant A connecting to tenant B's hostname → close 4401; stale `tv` → 4401; test proves both | — |
| D4-LC-2 | Heartbeat mixin: server sends `{"type":"ping"}` every 30s; client must `{"type":"pong"}`; 2 missed → close 4408; cancel the asyncio task + `group_discard` everything on disconnect | `infrastructure/websocket/consumers.py` | Communicator test: pong keeps connection alive past 60s (with patched interval); silence closes 4408; no group membership leaks after disconnect | — |
| D4-LC-3 | `NotificationConsumer` at `ws/notifications/`: on connect join `user.{id}` + `branch.{b}` for every active (non-revoked) RoleMembership; handler `notify.message` relays payload JSON to the socket | `apps/notifications/consumers.py`, `apps/notifications/routing.py` | Authenticated connect → joined groups match memberships; anonymous → 4401 | D4-LC-1, D4-LC-2 |
| D4-LC-4 | `AttendanceConsumer` at `ws/cohorts/<cohort_id>/attendance/`: permission check **on connect** — `has_permission_code(roles, "attendance:read")` AND (director or a RoleMembership branch == cohort.branch_id); deny → close 4403; join `cohort.{id}`; handler `attendance.update` relays | `apps/attendance/consumers.py`, `apps/attendance/routing.py` | Teacher in cohort's branch receives a relayed event; teacher from another branch → 4403; cross-tenant token → 4401 | D4-LC-1, D4-LC-2 |
| D4-LC-5 | Routing aggregation: `infrastructure/websocket/routing.py` concatenates per-app `websocket_urlpatterns` (`apps.notifications.routing`, `apps.attendance.routing`) and keeps `ws/ping/` | `infrastructure/websocket/routing.py` | All three paths route; `config/asgi.py` untouched | D4-LC-3, D4-LC-4 |
| D4-LC-6 | Wire dispatch → groups (TD-15): the in-app channel inside `apps.notifications.services.dispatch()` calls `infrastructure/websocket/channel_layer.group_send(f"user.{uid}", {"type":"notify.message","payload":...})`; attendance-marked events additionally `group_send(f"cohort.{cid}", {"type":"attendance.update", ...})` — **from dispatch, never from apps.attendance** | `apps/notifications/services.py` | E2E test: `dispatch(...)` for a user with an open `NotificationConsumer` delivers the payload over the socket; a grep-test asserts `channel_layer.group_send` is imported only under `apps/notifications/` + `infrastructure/websocket/` | D4-LC-3 |
| D4-LC-7 | Client reconnect guidance: append a "Realtime" section to `agents/API-CONTRACT.md` — subprotocol `bearer.<token>`, close codes (4401 auth, 4403 forbidden, 4408 heartbeat), exponential backoff 1s→30s with jitter, resubscribe-on-reconnect | `agents/API-CONTRACT.md` (additive) | Section exists, lists every close code the consumers emit | D4-LC-4 |

**Endpoints (WebSocket paths, tenant subdomain):**
- `wss://<tenant>/ws/notifications/` — any authenticated user — server pushes `{"type":"notification","payload":{...}}`.
- `wss://<tenant>/ws/cohorts/<cohort_id>/attendance/` — `attendance:read` + branch scope — pushes `{"type":"attendance.update","payload":{...}}`.
- `wss://<tenant>/ws/ping/` — smoke, unchanged.

**Signals/Celery:** none new — dispatch already runs in `celery_tasks/notification_tasks.py::dispatch_notification` (Day 3). Group sends are fire-and-forget; idempotency is the dispatch layer's (Day-3 dedupe key); a dropped socket simply misses the frame — the in-app feed (`GET /api/v1/notifications/`) is the source of truth on reconnect (say so in the API-CONTRACT section).

**Tests required (pytest-asyncio + `channels.testing.WebsocketCommunicator`, TESTING.md realtime rows):**
- [ ] anonymous connection rejected 4401 (TASKS §26)
- [ ] cross-tenant token rejected 4401 (TD-1 on WS)
- [ ] stale `tv` rejected 4401
- [ ] authenticated notification delivery E2E via `dispatch()` (TASKS §26 "receives hello" superseded by real payload)
- [ ] attendance branch-scope deny 4403
- [ ] heartbeat: pong sustains, silence closes 4408
- [ ] disconnect removes all group memberships
- [ ] producer-uniqueness grep test (TD-15)

**Publish to WORKLOG:** WS paths + close codes + message envelope (`{"type", "payload"}`); group naming (`user.{id}`, `cohort.{id}`, `branch.{id}`) — Lane B's `report.ready` and Day-5 demo rely on these.

---

## Lane D — Printing (apps/printing, TASKS §14, §28 server side)

**Objective:** Replace `PrintingItem` with `Printer`/`BranchAgent`/`PrintJob`, a hashed-token DRF authentication class for branch agents, atomic claim + status endpoints, retry policy, per-cohort/term quotas from `CenterSettings` (TD-13), audit events (TD-9), and enqueue hooks consumed by transcripts (Day 2), receipts (Day 3), and reports (Lane B). **No CUPS code** — the agent is a separate repo (TASKS §28, ADR-004).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D4-LD-1 | Models + migration; drop `PrintingItem` CRUD | `apps/printing/models.py`, `migrations/`, `admin.py` | Constraints below; `migrate_schemas` clean | — |
| D4-LD-2 | `BranchAgentAuthentication` (DRF `BaseAuthentication`): header `Authorization: Agent <raw-token>`; sha256 lookup against `BranchAgent.token_hash`; revoked/unknown → 401 envelope (TD-18); sets `request.auth = agent`. Companion `IsBranchAgent` permission. Token minted once via `services.register_agent(branch, name) -> raw token` (shown once, hash stored) | `apps/printing/authentication.py`, `apps/printing/services.py` | Valid token authenticates with zero User involvement; revoked token → 401 `agent_token_invalid`; raw token never persisted (test asserts no plaintext in DB) | D4-LD-1 |
| D4-LD-3 | Agent endpoints: `POST /api/v1/printing/agent/claim/` — `select_for_update(skip_locked=True)` oldest `queued` job for `request.auth.branch`, set `picked` + `claimed_at` + `agent`, return job + `presign_download(payload_s3_key)`; 204 when queue empty. `POST /api/v1/printing/agent/jobs/<id>/status/` `{status, error?, pages_printed?}` — transitions picked→printing→done|failed only; stamps `BranchAgent.last_seen_at` | `apps/printing/views.py`, `urls.py`, `serializers.py` | Two concurrent claims (threaded test) never return the same job; agent from branch X cannot claim/update branch Y's job (404); illegal transition → 409 `invalid_transition` | D4-LD-2 |
| D4-LD-4 | Retry policy: on `failed` with `attempts < 3` → back to `queued` with `next_attempt_at = now + 2^attempts * 60s` (claim query filters `next_attempt_at__lte=now`); 3rd failure is final → dispatch `print.failed` notification to `requested_by` + audit event | `apps/printing/services.py` | Test: 3 failures → final `failed`, exactly one notification, `attempts == 3`; backoff timestamps correct | D4-LD-3 |
| D4-LD-5 | Quotas (TD-13): `CenterSettings` gains `print_quota_pages_per_cohort_term: PositiveIntegerField` (0 = unlimited; additive migration in the app owning CenterSettings — coordinate via WORKLOG); `services.enqueue_print(...)` rejects when cohort's term usage + pages×copies exceeds it | `apps/printing/services.py`, CenterSettings app (additive) | Over-quota enqueue → `StarforgeError` code `print_quota_exceeded`; quota 0 never blocks | D4-LD-1 |
| D4-LD-6 | Enqueue hooks: public service `enqueue_print(*, source: str, source_id: int, payload_s3_key: str, branch_id: int, requested_by, pages: int, copies=1, color=False, duplex=False, cohort_id=None)` + implement `celery_tasks/print_tasks.py::enqueue_print_job` (marks queued, calls audit); wire callers: academics transcript-ready, payments receipt-ready, reports run-done (Lane B calls it post-merge — agree signature in WORKLOG by midday) | `apps/printing/services.py`, `celery_tasks/print_tasks.py` | Duplicate (source, source_id, payload key) enqueue within an open job is a no-op (idempotency); audit rows `print.job_created` / `print.job_done` carry pages | D4-LD-5 |
| D4-LD-7 | Staff endpoints + perms: `printing:read`/`printing:write` matrix entries (teacher write — request prints; director/registrar manage printers/agents) | `apps/printing/views.py`, `core/permissions.py` (additive) | Per-action perms; student 403 on job create; list filter by status/source/branch; query-count test | D4-LD-1 |

**Models:** `Printer` — `branch: FK(org.Branch, CASCADE)`, `name: CharField(120)`, `model_name: CharField(120, blank)`, `capabilities: JSONField default dict` (color/duplex/paper), `is_active: BooleanField default True`; `unique_together(branch, name)`. `BranchAgent` — `branch: FK(org.Branch, CASCADE)`, `name: CharField(120)`, `token_hash: CharField(64, unique)`, `created_by: FK(users.User, SET_NULL, null)`, `last_seen_at: DateTimeField null`, `revoked_at: DateTimeField null`, `created_at`. `PrintJob` — `branch: FK`, `printer: FK(Printer, SET_NULL, null)`, `agent: FK(BranchAgent, SET_NULL, null)`, `status: CharField(16, choices queued|picked|printing|done|failed, db_index)`, `source: CharField(16, choices assignment|transcript|report|receipt)`, `source_id: PositiveBigIntegerField`, `payload_s3_key: CharField(512)`, `pages: PositiveIntegerField`, `copies: PositiveSmallIntegerField default 1`, `color/duplex: BooleanField default False`, `cohort_id: PositiveBigIntegerField null` (no FK — quota lookup only), `requested_by: FK(users.User, SET_NULL, null)`, `attempts: PositiveSmallIntegerField default 0`, `next_attempt_at: DateTimeField null, db_index`, `last_error: TextField blank`, `created_at/claimed_at/finished_at`. Index `(branch, status, next_attempt_at)`.

**Endpoints to expose:**
- `POST /api/v1/printing/jobs/` — `printing:write` — create job (staff path; service applies quota) → 201 job.
- `GET /api/v1/printing/jobs/` — `printing:read` — filters: status, source, branch; ordered `-created_at`.
- `GET|POST /api/v1/printing/printers/` — `printing:write` for POST, `printing:read` for GET.
- `POST /api/v1/printing/agents/` — `printing:write` (director/registrar) — 201 `{id, token}` (token shown once).
- `POST /api/v1/printing/agent/claim/` — `IsBranchAgent` (no JWT) — 200 `{job, download_url}` or 204.
- `POST /api/v1/printing/agent/jobs/<id>/status/` — `IsBranchAgent` — 200 updated job; 409 `invalid_transition`.

**Signals/Celery + idempotency:** `enqueue_print_job` is idempotent on open `(source, source_id, payload_s3_key)`; requeue-on-failure uses `next_attempt_at`, never a Celery countdown (jobs are pulled by agents, not pushed). Audit events via the Day-3 `audit_log()` helper (TD-9); `print.failed` notification via `dispatch()` only.

**Tests required (TESTING.md matrix):**
- [ ] concurrent claim atomicity (`select_for_update(skip_locked=True)` — two claims, one job each)
- [ ] agent auth: valid / revoked / unknown token; cross-branch claim 404
- [ ] full transition matrix incl. illegal jumps → 409
- [ ] retry exhaustion: 3 failures → final failed + one notification + audit rows
- [ ] quota edge: exactly-at-limit allowed, one page over → `print_quota_exceeded`
- [ ] cross-tenant isolation on jobs list; permission denials per role; query-count on list

**Publish to WORKLOG:** `enqueue_print(...)` exact signature (Lane B + Day-5 demo consume); agent auth header format + claim/status payloads (separate-repo agent team contract, TASKS §28).

---

## Lane E — Control center (TD-10, TASKS §2 public-schema-admin)

**Objective:** Platform API on the **public schema** under `/api/v1/platform/`: full Center lifecycle, per-center usage (consumes Lane B's snapshots + live DAU), subscription management (apps/billing from Day 3), read-only impersonation (10-min scoped JWT, audited both sides), TD-19 resolve endpoint, and platform Django admin polish. Note: ROADMAP §6 lists TD-19 under D5-D for handoff docs — the *endpoint* ships today so D5-D only documents it.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D4-LE-1 | Center lifecycle API: extend `CenterViewSet` (`apps/tenancy/views.py`) to CRUD (create delegates to `services.provision_center`) + actions `POST .../suspend/`, `POST .../activate/`, `POST .../extend-trial/ {days}`; permission `IsAdminUser` on public-schema users (TD-3) | `apps/tenancy/views.py`, `serializers.py`, `services.py`, `urls.py` | Suspend flips `is_active=False` → tenant API returns 503/402 per Day-3 paywall; extend-trial moves `trial_ends_at`; non-staff public user → 403; tenant-JWT user → 401 (different user table) | — |
| D4-LE-2 | Usage endpoint: `GET /api/v1/platform/centers/<id>/usage/?days=30` → `{dau, students, storage_bytes, ai_tokens}` time series from `billing.UsageSnapshot` + today's live DAU computed under `schema_context` from `users.User.last_seen_at` | `apps/tenancy/views.py`, `apps/billing/selectors.py` | Returns Lane B's nightly rows + a `today` live point; two-tenant test shows no bleed | D4-LB-7 (snapshot fields per WORKLOG) |
| D4-LE-3 | Subscription management: `GET /api/v1/platform/subscriptions/`, `PATCH .../<id>/` (plan change, status override), mounted via `apps/billing/urls_platform.py` + additive `config/urls_public.py` include | `apps/billing/views.py`, `apps/billing/urls_platform.py`, `config/urls_public.py` | Reactivating a suspended subscription makes tenant API return 200 again (paywall test reused from Day 3); all mutations audited (D4-LE-5) | Day-3 billing |
| D4-LE-4 | Read-only impersonation: `POST /api/v1/platform/centers/<id>/impersonate/ {user_id}` → 10-min access token with claims `{schema: center.schema_name, impersonator_id, read_only: true, tv}` minted via `apps/tenancy/services.mint_impersonation_token`; enforcement: `core/authentication.py` (TD-1 class) surfaces the claim, new `core/permissions.py::DenyWriteForReadOnlyToken` (in `TenantSafeModelViewSet.permission_classes`) → 403 code `read_only_token` on non-SAFE methods | `apps/tenancy/services.py`, `views.py`, `core/authentication.py` (additive), `core/permissions.py` (additive), `core/viewsets.py` | Impersonation token GETs tenant data successfully; any POST/PATCH/DELETE → 403 `read_only_token`; token expires ≤10 min; refresh impossible (access-only); normal tokens (no claim) unaffected — full Day-1..3 suite still green | — |
| D4-LE-5 | Audit both sides: new public-schema model `PlatformEvent(actor: FK public users.User, center: FK Center null, event: CharField(64), payload: JSONField, created_at)` in `apps/tenancy`; write on suspend/activate/extend-trial/subscription-change/impersonation-mint; ALSO `audit_log()` (Day-3 D helper) inside the target tenant schema on impersonation mint (`impersonation.started`, includes `impersonator_id`) | `apps/tenancy/models.py`, `services.py`, `migrations/` | Minting impersonation creates exactly 1 PlatformEvent + 1 tenant AuditLog row; append-only (no update/delete API) | D4-LE-4 |
| D4-LE-6 | TD-19 resolve: `GET /api/v1/platform/resolve/?slug=demo` (AllowAny, anon-throttled) → `{name, base_url, ws_url, logo, locale}` built from `Domain` rows; unknown slug → 404 envelope | `apps/tenancy/views.py`, `urls.py` | Anonymous request resolves the seeded `demo` center; throttle test (anon rate) passes | — |
| D4-LE-7 | Admin polish + apex lockdown: Center `list_display` gains latest-snapshot usage columns + Subscription inline; verify apex `/admin/` only authenticates public-schema staff (TD-3) with an explicit test | `apps/tenancy/admin.py`, `apps/billing/admin.py` | Admin renders with usage columns; tenant-schema user credentials fail on apex admin login (test) | D4-LB-7 |

**Endpoints to expose (all public-schema, `IsAdminUser` on TD-3 platform users unless noted):**
- `GET|POST|PATCH /api/v1/platform/centers/` — CRUD; POST → `provision_center`.
- `POST /api/v1/platform/centers/<id>/suspend/` · `.../activate/` · `.../extend-trial/ {days}` — 200 center.
- `GET /api/v1/platform/centers/<id>/usage/?days=30` — `{series: [{date, dau, students, storage_bytes, ai_tokens}], today: {...}}`.
- `POST /api/v1/platform/centers/<id>/impersonate/ {user_id}` — 200 `{access, expires_in: 600}` (no refresh).
- `GET /api/v1/platform/subscriptions/` + `PATCH /api/v1/platform/subscriptions/<id>/`.
- `GET /api/v1/platform/resolve/?slug=demo` — **AllowAny**, anon-throttled — `{name, base_url, ws_url, logo, locale}` (TD-19).

**Signals/Celery:** none new — lifecycle mutations are synchronous public-schema writes; trial expiry + metering stay with Day-3 billing tasks (rows in Lane F's beat table). Every mutation writes a `PlatformEvent` (append-only, no delete API).

**Tests required (TESTING.md matrix):**
- [ ] suspend → tenant API 402/503 → activate → 200 round trip (reuses Day-3 paywall fixtures)
- [ ] impersonation: GET 200, POST/PATCH/DELETE 403 `read_only_token`, expired token 401, no refresh path
- [ ] both-sides audit: 1 `PlatformEvent` + 1 tenant `AuditLog` per mint
- [ ] resolve: happy, unknown slug 404 envelope, anon throttle
- [ ] apex admin lockdown: tenant-schema credentials rejected (TD-3)
- [ ] usage endpoint two-tenant isolation + non-staff 403; full Day-1..3 suite still green (read-only wrapper is non-breaking)

**Publish to WORKLOG:** impersonation claim shape (`read_only`, `impersonator_id`) — Day-5 A security review and D5-D API contract need it; PlatformEvent model; final platform URL map.

---

## Lane F — i18n (TASKS §24) + beat consolidation (TASKS §22)

**Objective:** Sweep every app for untranslated user-facing strings, generate/compile uz/en/ru catalogs (compile in CI), verify template language variants and `preferred_language` consumption, then consolidate ALL periodic tasks into one documented `CELERY_BEAT_SCHEDULE` with DLQ + duration logging. Merges **last** — rebase on all lanes before the sweep.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D4-LF-1 | `gettext_lazy` sweep: model `verbose_name`s, `choices` labels, validation/error messages in `apps/*`, `core/exceptions.py`, `core/validators.py` | all apps (string-only edits) | `grep` audit script (commit it as `scripts/check_i18n.py`) finds zero bare user-facing literals in serializer/service error paths; ruff/mypy clean | all lanes merged |
| D4-LF-2 | Catalogs: `manage.py makemessages -l uz -l en -l ru`, translate uz first (en source, ru best-effort), commit `locale/`; CI gains a `compilemessages` step (additive job step, not a new job) | `locale/`, `.github/workflows/ci.yml` | CI green with compile step; `activate("uz")` test shows a translated validation message | D4-LF-1 |
| D4-LF-3 | Language plumbing: verify `LocaleMiddleware` order in `config/settings/base.py` (after Session, before Common — already correct; assert with a test); `Accept-Language` honored on API error messages; `users.User.preferred_language` exists (Day-1 §3 item — if missing, add additive migration: `CharField(8, choices=settings.LANGUAGES, default="uz")`); `notifications.dispatch` picks template variant by `preferred_language` with center-default fallback | `apps/users/` (additive if needed), `apps/notifications/services.py`, tests | Dispatch to a `ru` user renders the ru template (Day-3 templates); missing variant falls back to uz and logs a warning; completeness test: every NotificationTemplate event has uz+en+ru rows | D4-LF-1 |
| D4-LF-4 | Beat consolidation: define `CELERY_BEAT_SCHEDULE` in `config/settings/base.py` (DatabaseScheduler syncs code-defined entries into django-celery-beat at startup) with the full table below; ensure every `celery_tasks/*_tasks.py` module is imported in `celery_tasks/__init__.py` so `autodiscover_tasks(["celery_tasks"])` actually registers them (today `__init__.py` is empty — verify registration via `app.tasks` keys in a test) | `config/settings/base.py`, `celery_tasks/__init__.py` | Test asserts every table row's task name is in `app.tasks` and in `CELERY_BEAT_SCHEDULE`; no other module defines ad-hoc periodic entries (grep test) | all lanes |
| D4-LF-5 | DLQ + metrics (TASKS §22): `task_failure` signal handler pushes `{task, args, exc, schema}` to Redis list `starforge:dlq` after retries exhaust; `task_prerun/postrun` handlers log duration (structured, tenant-tagged via existing `TenantSchemaFilter`); document drain procedure in `docs/` | `config/celery.py`, `docs/architecture.md` (section) | Forced-failure test lands one DLQ entry; duration appears in captured logs | D4-LF-4 |

**Consolidated beat table (this exact table goes into the settings docstring / docs):**

| Task (module::name) | Schedule | Scope | Idempotency |
|---|---|---|---|
| `cleanup_tasks::purge_expired_otps` | daily 03:00 | per-tenant iterate | delete-by-filter, naturally idempotent |
| `attendance::mark_absent_after_lesson` (Day-2 B name per WORKLOG) | every 15 min | per-tenant | skips lessons already having records |
| `finance::late_payment_reminders` (Day-3 A) | daily 09:00 | per-tenant | dedupe key per invoice+day in dispatch |
| `assignments::assignment_due_soon` (Day-2 D) | daily 17:00 | per-tenant | dedupe key per assignment+user |
| `billing::meter_usage_and_flip_states` (Day-3 E, TD-8) | daily 01:00 | public | upsert by (center, date) |
| `report_tasks::nightly_platform_aggregation` (D4-LB-7) | daily 02:00 | public | upsert by (center, date) |
| `report_tasks::run_due_report_schedules` (D4-LB-6) | hourly | per-tenant | `last_run_at` guard |
| `billing::expire_trials` (Day-3 E / TASKS §2) | daily 00:30 | public | state-check before flip |
| `audit::cleanup_old_audit_logs` (Day-3 D retention) | weekly Sun 04:00 | per-tenant | delete-by-filter |
| `cleanup_tasks::flush_expired_jwt_blacklist` (wraps `flushexpiredtokens`) | weekly Sun 04:30 | per-tenant | delete-by-filter |

Per-tenant tasks iterate `get_tenant_model().objects.exclude(schema_name="public")` inside `schema_context` (pattern already used by tenant-schemas-celery — keep one iteration helper in `celery_tasks/__init__.py`).

**Endpoints:** none new. Lane F verifies `PATCH /api/v1/users/me/ {preferred_language}` works (Day-1 profile endpoint) — if the field is missing from the write serializer, add it.

**Tests required (TESTING.md matrix):**
- [ ] beat completeness: every table row registered in `app.tasks` AND present in `CELERY_BEAT_SCHEDULE`
- [ ] `activate("uz")` renders a translated validation message; ru → uz fallback logs a warning
- [ ] dispatch uses `preferred_language` template variant (uz/en/ru template completeness assert)
- [ ] DLQ receives exactly one entry on exhausted retries; duration logged on success
- [ ] LocaleMiddleware order + `Accept-Language` honored on an API error response

**Publish to WORKLOG:** final beat table (authoritative — supersedes any per-lane ad-hoc schedule notes); any task name that differed from Day-2/3 WORKLOG announcements; i18n string-coverage stats per app.

---

## Cross-lane & cross-day integration points

| # | Producer (day-lane) | Consumer (day-lane) | Interface | Test that proves it |
|---|---|---|---|---|
| 1 | D2-D assignments `submission_created` signal | D4-A `apps/ai/receivers.py` | Django signal, payload `submission_id` | Submission create → exactly one `run_assignment_feedback` enqueued (D4-LA-7) |
| 2 | D2-E content upload-confirmed signal | D4-A content summary | signal → `run_content_summary` | upload confirm → AIRequest row created |
| 3 | D4-A `apps.ai.selectors.tokens_consumed(start,end)` | D4-B `ai_usage` generator + D4-LB-7 aggregation + D3-E billing metering | tenant-schema selector, int | aggregation test writes correct `ai_tokens` into UsageSnapshot |
| 4 | D2-E storage (`LessonFile.size_bytes`, `{schema}/` prefix) | D4-B `storage_usage` generator + D4-LB-7 | selector sum | snapshot `storage_bytes` matches factory data |
| 5 | D4-B `nightly_platform_aggregation` → `billing.UsageSnapshot` | D4-E usage endpoint + admin columns | UsageSnapshot rows (fields per WORKLOG) | `GET /platform/centers/<id>/usage/` returns the nightly rows (D4-LE-2) |
| 6 | D3-C `notifications.dispatch()` | D4-C group_send (TD-15: ONLY producer) | `dispatch()` → `group_send("user.{id}", ...)` | E2E socket delivery (D4-LC-6) + producer-uniqueness grep test |
| 7 | D2-C transcripts / D3-B receipts / D4-B reports | D4-D `enqueue_print(...)` | service call, signature in WORKLOG by midday | transcript-ready creates a queued PrintJob (post-merge integration test, Lane D writes it) |
| 8 | D3-E paywall middleware | D4-E suspend/activate | Subscription status flip | suspend → 402 on tenant API → activate → 200 (D4-LE-1/3) |
| 9 | D1-C TD-1 auth class | D4-C WS middleware, D4-E impersonation | `schema`/`tv`/`read_only` claims | cross-tenant WS reject; read-only write 403 |
| 10 | all lanes' periodic tasks | D4-F consolidated schedule | `CELERY_BEAT_SCHEDULE` | completeness test (D4-LF-4) |

Coordination rules today: Lane A announces `tokens_consumed` and Lane D announces `enqueue_print` in WORKLOG **by midday** (consumers code against the announcement, integration-test after merge). Lane B owns any additive `UsageSnapshot` migration; Lane E only reads. Lane F rebases on everything and merges last.

---

## EOD gate — 100% green before Day 4 closes

- [ ] `uv run ruff check . && uv run ruff format --check .` clean on `master` after final merge.
- [ ] `uv run mypy apps core infrastructure config` clean.
- [ ] `uv run pytest -q` green; `uv run pytest --cov=apps --cov=core --cov-fail-under=80 -q` passes (TD-20; Day-5 raises to 85).
- [ ] Migration check: fresh DB → `migrate_schemas --shared` + provisioning a new Center runs all tenant migrations without conflict (renumber per ROADMAP §2.3 if two lanes collided).
- [ ] OpenAPI: schema generation CI job green; new endpoints have `@extend_schema` summaries/examples (DoD #7).
- [ ] Demo script (run against seeded `demo` tenant, mock flags on):
  1. Open `/ws/notifications/` with a valid token (subprotocol `bearer.<token>`); mark a student absent → in-app payload arrives on the socket and on `/ws/cohorts/<id>/attendance/`.
  2. Student submits an assignment → `AIRequest` row `succeeded` (mock), feedback text saved, `tokens_used_today` increased; exhaust the budget → next submission yields `denied_budget`.
  3. `POST /api/v1/reports/runs/` (attendance, pdf) → run reaches `done`, `download_url` fetches a PDF from MinIO; `report.ready` notification recorded.
  4. `enqueue_print` a transcript → `POST /api/v1/printing/agent/claim/` with an agent token returns it with a presigned URL → status to `done`; audit rows present.
  5. Platform staff on apex: suspend `demo` → tenant API 402; activate → 200; `GET /platform/centers/<id>/usage/` shows snapshot data; mint impersonation token → GET works, PATCH → 403 `read_only_token`; `GET /api/v1/platform/resolve/?slug=demo` resolves anonymously.
  6. `manage.py shell`: every beat-table task present in `app.tasks`; force one task failure → entry in `starforge:dlq`.
  7. Switch a user to `ru` → dispatched notification uses the ru template.
- [ ] TASKS.md ticked: §14 (server side), §18, §20, §21, §22 (consolidation rows), §24, §2 public-schema-admin items; partial items marked `[~]` with `BLOCKED(O-x)` notes ([OWNER:O-2] real Anthropic key, [OWNER:O-7] real push for WS-adjacent push fan-out — mocks shipped per TD-2).
- [ ] WORKLOG: one entry per lane, each publishing the interfaces named in its "Publish to WORKLOG" block; deviations bolded.
- [ ] Merge order honored A→B→C→D→E→F; CI green on `master`.
