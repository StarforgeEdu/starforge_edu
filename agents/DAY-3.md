# DAY 3 — Money, Fiscal & Messaging

At start of day: people domain, org, auth/JWT (TD-1/4/5), `CenterSettings` (TD-13), and the Day-2 academic engine (schedule, attendance, academics, assignments, content/storage) are live and migrated; `apps/finance`, `apps/payments`, `apps/notifications`, `apps/audit` still contain placeholder `<App>Item` models. By EOD: full invoice→payment→fiscal-receipt pipeline works against mocks, every signal emitted on Days 1–3 lands as a notification, the audit trail is append-only and queryable, and the platform paywall returns 402 for suspended tenants. Coverage floor rises to **80%** (TD-20).

## Prerequisites

Verify before branching. If any fails, fix or escalate per ROADMAP §2.4 — do not build on sand.

- [ ] Read the last 2 days of `agents/WORKLOG.md`. Collect the **exact signal names** published by D1-C (new-device login), D1-D (enrollment state machine), D2-B (absence), D2-C (grades published), D2-D (assignment created/due/graded). Lane A and Lane C depend on these names verbatim.
- [ ] `uv run pytest -q` green on master; `uv run python manage.py makemigrations --check --dry-run` reports no missing migrations.
- [ ] `core/fields.py` exists with `EncryptedCharField`/`EncryptedTextField` (TD-11, landed Day 1 for `national_id`/`medical_notes`). If missing, Lane B creates it as task D3-B-0 and flags `[OWNER:O-11]` for the production `FIELD_ENCRYPTION_KEY`.
- [ ] `CenterSettings` (D1-B) exposes: currency pair + FX source, quiet hours, grading scheme. Lane A/C read it — confirm field names in WORKLOG, never hardcode.
- [ ] Smoke: OTP login on `demo.localhost`, `GET /api/v1/students/` returns 200 with seeded data.
- [ ] `scripts/seed_dev.py` runs clean (lanes extend it today; rebase before editing).

Merge order today (ROADMAP §2.3): **A → B → C → D → E → F**. B imports A's services; C consumes A/B signals; D audits A/B models; F tests everything.

---

## Lane A — Finance (apps/finance)

**Objective.** Replace `FinanceItem` with the real billing ledger: fee schedules, invoices with FX snapshot, discounts/scholarships/payment plans, payment allocation, cashier shifts, statements. Implements TASKS §15, parts of §22 (`late_payment_reminders`). TD-13, TD-14, TD-18, TD-20.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D3-A-1 | Models below + migration; delete `FinanceItem` | `apps/finance/models.py`, `apps/finance/migrations/`, `apps/finance/admin.py` | `migrate_schemas` clean on fresh DB; constraints listed below enforced at DB level | — |
| D3-A-2 | `services.issue_invoice(student_id, fee_schedule_id=None, lines=None)` — numbering, FX snapshot, discount materialization | `apps/finance/services.py` | Invoice number matches `INV-{YYYY}-{seq:06d}` per center, unique; `fx_rate_usd` + `total_usd` frozen at issue; sibling discount auto-applied when `CenterSettings.sibling_discount_percent > 0` and student shares an active `parents.Guardian` with another enrolled student | D3-A-1 |
| D3-A-3 | Auto-issue on enrollment: receiver on the D1-D enrollment signal (name from WORKLOG, e.g. `student_enrolled`) | `apps/finance/receivers.py`, `apps/finance/apps.py` (`ready()`) | Enrolling a student with an active matching `FeeSchedule` creates exactly one `issued` invoice; re-firing the signal does not duplicate (dedupe on `(student, fee_schedule, period)`) | D3-A-2 |
| D3-A-4 | `services.allocate_payment(payment_id: int, amount_uzs: Decimal, invoice_ids: list[int] | None)` — oldest-due-first auto split, exact Decimal accounting | `apps/finance/services.py` | Sum of created `PaymentAllocation.amount_uzs` == `amount_uzs` exactly (no rounding loss); invoice flips `issued→partially_paid→paid` correctly; over-allocation raises `ValidationException` | D3-A-1 |
| D3-A-5 | Cashier shift open/close + daily report selector | `apps/finance/services.py`, `apps/finance/selectors.py` | Open requires no other open shift for that cashier (409-style `ValidationException`); close computes `discrepancy = closing_cash - (opening_cash + cash payments in shift)`; report endpoint returns totals per provider | D3-A-1, D3-B-1 (Payment FK lands after merge — report tolerates zero payments) |
| D3-A-6 | `selectors.outstanding_balance(student_id) -> Decimal` + endpoint; parent scoping | `apps/finance/selectors.py`, `views.py` | issued+partially_paid totals minus allocations; parent with `finance:read_own` sees only linked students (queryset scoped via Guardian); ≤3 queries | D3-A-4 |
| D3-A-7 | Statement-of-account PDF (weasyprint, Celery → S3 → signed URL, TD-14) | `celery_tasks/finance_tasks.py` (NEW), `templates/documents/statement_uz.html` (+`_ru`,`_en`), `apps/finance/views.py` | `POST .../statement/` returns 202 + task id; task writes PDF to S3 under `{schema_name}/documents/`, result endpoint returns signed URL; template renders invoice lines + allocations + balance | D3-A-6 |
| D3-A-8 | `late_payment_reminders` daily beat task → signal | `celery_tasks/finance_tasks.py`, `config/celery.py` (append to `app.conf.beat_schedule` only), `apps/finance/signals.py` | Scans `due_date < today`, status `issued/partially_paid`; emits `payment_reminder` signal once per invoice per `CenterSettings.payment_reminder_interval_days` (default 3); re-running same day sends nothing (dedupe key on dispatch) | D3-A-1, D3-C-3 |
| D3-A-9 | ViewSets + per-action `required_perms` (TD-5), filters, `@extend_schema`; seed `FeeSchedule` + 2 invoices in `scripts/seed_dev.py` | `apps/finance/views.py`, `serializers.py`, `urls.py`, `scripts/seed_dev.py` | All endpoints below respond per spec; OpenAPI schema job green; no `fields = "__all__"` | D3-A-1..6 |

**Models** (all tenant schema; every FK `db_index` implied; money = `DecimalField(max_digits=18, decimal_places=2)` in UZS):

- `FeeSchedule` — name: Char(120); cohort: FK cohorts.Cohort null (null = center-wide default); amount_uzs: Decimal; billing_period: Char choices monthly/term/one_time; due_day_of_month: PositiveSmallInt default 5; is_active: Bool; created_at. Constraint: amount_uzs ≥ 0.
- `Invoice` — number: Char(32) unique; student: FK students.StudentProfile (PROTECT); cohort: FK cohorts.Cohort null; fee_schedule: FK FeeSchedule null SET_NULL; status: Char choices draft/issued/partially_paid/paid/void/overdue (db_index); issue_date: Date; due_date: Date (db_index); currency: Char(3) default "UZS"; total_uzs: Decimal; fx_rate_usd: Decimal(12,4) null (snapshot, source per `CenterSettings` FX config — "cbu" rate fetched by Celery mock-first, or "manual" rate field); total_usd: Decimal null; created_by: FK users.User null SET_NULL; created_at/updated_at. Constraints: total_uzs ≥ 0; `UniqueConstraint(number)`.
- `InvoiceLine` — invoice: FK CASCADE related_name "lines"; description: Char(255); line_type: Char choices tuition/material/penalty/discount/other; quantity: Decimal(8,2) default 1; unit_price_uzs: Decimal; amount_uzs: Decimal (negative allowed only for line_type=discount — CheckConstraint).
- `Discount` — student: FK students.StudentProfile; discount_type: Char choices sibling/scholarship/manual; percent: Decimal(5,2) null; fixed_amount_uzs: Decimal null; valid_from/valid_until: Date; approved_by: FK users.User null; is_active: Bool. CheckConstraint: exactly one of percent/fixed_amount_uzs set. Materializes as a negative `InvoiceLine` at issue time.
- `PaymentPlan` — invoice: OneToOne FK; created_by: FK users.User null. `PaymentPlanInstallment` — plan: FK related_name "installments"; due_date: Date; amount_uzs: Decimal; status: Char pending/paid/overdue. Constraint: installments must sum to invoice.total_uzs (validated in service).
- `PaymentAllocation` — invoice: FK CASCADE related_name "allocations"; **payment_id: BigIntegerField db_index (soft reference — see decision note)**; amount_uzs: Decimal > 0; created_at.
- `Refund` — invoice: FK PROTECT; **payment_id: BigIntegerField null db_index (soft reference)**; amount_uzs: Decimal > 0; reason: Char(255); state: Char choices requested/approved/sent_to_provider/completed/failed (db_index); requested_by/approved_by: FK users.User null; created_at/updated_at. State transitions enforced in `services.transition_refund()` — illegal jump raises `ValidationException`.
- `CashierShift` — cashier: FK users.User PROTECT; branch: FK org.Branch PROTECT; status: Char open/closed; opened_at/closed_at; opening_cash_uzs/closing_cash_uzs: Decimal; discrepancy_uzs: Decimal null; notes: Text blank.

> **Decision (cross-lane FK avoidance):** `PaymentAllocation.payment_id` and `Refund.payment_id` are plain BigIntegers, NOT FKs to `payments.Payment` — Lane A's migration must not depend on Lane B's same-day migration. Lane B's `Payment` may FK `finance.CashierShift` because B merges after A. Document in WORKLOG.

**Endpoints** (all `TenantSafeModelViewSet`/APIView, per-action `required_perms`):

| Method + path | Perm | Response |
|---|---|---|
| GET/POST `/api/v1/finance/fee-schedules/` | finance:read / finance:write | paginated list / created object |
| GET/POST `/api/v1/finance/invoices/`, GET `/{id}/`, POST `/{id}/void/` | finance:read / finance:write | invoice with nested lines + allocations |
| GET/POST `/api/v1/finance/discounts/` | finance:write | discount objects |
| POST `/api/v1/finance/invoices/{id}/payment-plan/` | finance:write | plan with installments |
| GET `/api/v1/finance/outstanding/?student=<id>` | finance:read (parent: finance:read_own, queryset-scoped) | `{student, outstanding_uzs, invoices:[...]}` |
| POST `/api/v1/finance/cashier-shifts/open/`, POST `/{id}/close/`, GET `/{id}/report/` | finance:write (cashier role allowed) / finance:read | shift object / report totals |
| POST `/api/v1/finance/students/{id}/statement/` → 202 `{task_id}`; GET `/api/v1/finance/statements/{task_id}/` → `{url}` | finance:read | TD-14 async pattern |

**Signals/Celery:** `apps/finance/signals.py` defines `invoice_issued = Signal()` (kwargs: invoice_id, student_id) and `payment_reminder = Signal()` (kwargs: invoice_id, student_id). Sent from services only. Beat: `late_payment_reminders` daily 09:00 Asia/Tashkent. Idempotency: reminder dedupe via Lane C `dedupe_key=f"finance.payment_reminder:{invoice_id}:{date}"`.

**Tests required** (per `agents/TESTING.md` matrix): happy-path invoice issue; auto-issue-on-enrollment fires once; allocation exactness incl. 3-way split of an odd amount; cashier shift double-open rejected; parent sees only own children's balances; cross-tenant isolation on `/invoices/`; query-count on invoice list (≤5 queries).

**Publish to WORKLOG:** `services.allocate_payment` signature, `services.register_refund_completion(refund_id, payment_id)` for Lane B, `invoice_issued`/`payment_reminder` signal names + kwargs for Lane C, soft-FK decision, exact `CenterSettings` fields consumed (FX source, sibling_discount_percent, payment_reminder_interval_days — add to CenterSettings if absent, additive migration in its owning app coordinated via WORKLOG).

---

## Lane B — Payments, Webhooks & Fiscalization (apps/payments, infrastructure/payments, infrastructure/fiscal)

**Objective.** Real provider integrations (Click/Payme/Uzum) mock-first, public-schema webhook intake per TD-6, Soliq fiscalization per TD-7, idempotency + replay protection end-to-end. Implements TASKS §16, TD-2, TD-6, TD-7, TD-11, TD-18. `[OWNER:O-3][OWNER:O-4][OWNER:O-5][OWNER:O-6]` — nothing blocks; mocks are the Day-3 deliverable.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D3-B-1 | Models below + migration; delete `PaymentItem` | `apps/payments/models.py`, `migrations/`, `admin.py` | `ProviderConfig` credential fields use `EncryptedCharField` (TD-11); migrate clean | core/fields.py |
| D3-B-2 | Click client: `prepare`/`complete` + md5 signature verify, plus `MockClickClient` (settings `CLICK_USE_MOCK`, default True) | `infrastructure/payments/click.py`, `config/settings/base.py` (append env keys only) | Sign string `md5(click_trans_id + service_id + SECRET_KEY + merchant_trans_id [+ merchant_prepare_id on complete] + amount + action + sign_time)` verified; bad sign → error `-1`; mock is deterministic (fixed ids derived from input) | D3-B-1 |
| D3-B-3 | Payme client: full JSON-RPC handler — CheckPerformTransaction, CreateTransaction, PerformTransaction, CancelTransaction, CheckTransaction, GetStatement + `MockPaymeClient` (`PAYME_USE_MOCK`) | `infrastructure/payments/payme.py` | **Spec compliance:** HTTP Basic `Paycom:<key>` else `-32504`; amounts in **tiyin** (`int(total_uzs * 100)`), mismatch → `-31001`; `account` object passed through and echoed, unknown invoice → error in **-31050..-31099** with `data` naming the field; transaction states 1/2/-1/-2; all times in ms; unknown method → `-32601`; CreateTransaction idempotent on Payme `id`; second concurrent transaction for same account → `-31099`; errors returned as JSON-RPC `error` member with **HTTP 200** | D3-B-1 |
| D3-B-4 | Uzum webhook client + `MockUzumClient` (`UZUM_USE_MOCK`) | `infrastructure/payments/uzum.py` | HMAC signature verify; deterministic mock | D3-B-1 |
| D3-B-5 | Public-schema webhook routes + views | `apps/payments/webhook_views.py` (NEW), `apps/payments/webhook_urls.py` (NEW), `config/urls_public.py` (append ONE line: `path("api/v1/webhooks/", include("apps.payments.webhook_urls"))`) | `POST /api/v1/webhooks/<provider>/<center_slug>/` resolves Center by slug (404 if absent/inactive), enters `schema_context`, loads that tenant's `ProviderConfig`, verifies signature, processes. Views are plain `APIView` with `authentication_classes = []`, `permission_classes = []` — **NOT `TenantSafeModelViewSet`**, whose `initial()` raises `TenantContextMissing` on the public schema (see `core/viewsets.py`); auth here is the provider signature, not JWT. Document this in the module docstring | D3-B-2..4 |
| D3-B-6 | Idempotency + replay protection | `apps/payments/services.py` | `Payment.idempotency_key` unique — same key twice returns the existing Payment (no duplicate row); `WebhookEvent` unique on `(provider, event_id)` — replayed nonce → recorded as `duplicate`, side effects run zero times | D3-B-1 |
| D3-B-7 | Checkout + allocation wiring | `apps/payments/services.py`, `views.py` | `create_checkout(invoice_id, provider)` returns redirect/payload from client; on provider "performed": Payment→completed, then `apps.finance.services.allocate_payment(...)` auto when amount matches a single invoice exactly, else `allocation_status=manual_review` + manual endpoint | D3-A-4 (merged) |
| D3-B-8 | Refund flow | `apps/payments/services.py` | Drives `finance.Refund` state machine via Lane A's service; Payme `CancelTransaction` (state -2) creates/completes a Refund with reason code stored; refund on non-completed Payment rejected | D3-A (merged) |
| D3-B-9 | Soliq fiscal client + post-payment task | `infrastructure/fiscal/__init__.py`, `infrastructure/fiscal/soliq_client.py` (NEW: `FiscalClient` ABC, `MockSoliqClient`, `SoliqClient`, `get_fiscal_client()`, `SOLIQ_USE_MOCK` default True), `celery_tasks/payment_tasks.py` (NEW) | Payment completed → `fiscalize_payment.delay(payment_id)`; task idempotent (existing confirmed `FiscalReceipt` short-circuits); retries max 3 exponential backoff; mock returns deterministic fiscal_sign + QR URL. `[OWNER:O-5]` | D3-B-1 |
| D3-B-10 | Daily reconciliation report + receipt PDF | `apps/payments/selectors.py`, `celery_tasks/payment_tasks.py`, `templates/documents/receipt_uz.html` (+ru/en) | `GET /api/v1/payments/reconciliation/?date=` returns payments vs allocated totals + mismatch list; receipt PDF via Celery→S3→signed URL (TD-14) | D3-B-7 |
| D3-B-11 | Signals: `payment_completed`, `payment_failed` | `apps/payments/signals.py` | Emitted exactly once per state transition (guard on previous status); kwargs: payment_id, invoice_id, student_id, amount_uzs | D3-B-7 |

**Models** (tenant schema):

- `ProviderConfig` — provider: Char choices click/payme/uzum (UniqueConstraint per tenant); is_active: Bool; click_service_id/click_merchant_id: Char blank; click_secret_key: EncryptedChar; payme_merchant_id: Char blank; payme_key/payme_test_key: EncryptedChar; uzum_merchant_id: Char blank; uzum_api_key: EncryptedChar; created_at/updated_at. Serializer: credential fields **write-only**, never echoed.
- `Payment` — provider: Char choices cash/click/payme/uzum/bank_transfer (db_index); amount_uzs: Decimal(18,2); currency: Char(3) default UZS; status: Char pending/processing/completed/failed/cancelled/refunded (db_index); idempotency_key: Char(64) **unique**; provider_txn_id: Char(64) blank db_index; provider_state: SmallInt null (Payme 1/2/-1/-2); provider_created_at_ms: BigInt null; cancel_reason: SmallInt null; account_ref: Char(64) blank (what the payer entered, e.g. invoice number); allocation_status: Char auto/manual_review/allocated; cashier_shift: FK finance.CashierShift null SET_NULL; payer: FK users.User null; paid_at: DateTime null; metadata: JSON default dict; created_at/updated_at.
- `PaymentAttempt` — payment: FK CASCADE related_name "attempts"; attempt_no: PositiveSmallInt; request_payload/response_payload: JSON; error_code: Char(32) blank; created_at.
- `WebhookEvent` — provider: Char; event_id: Char(128) (provider txn id / Payme id / nonce); UniqueConstraint(provider, event_id); signature_valid: Bool; status: Char received/processed/rejected/duplicate; payload: JSON; remote_ip: GenericIP null; processed_at: DateTime null; created_at.
- `FiscalReceipt` — payment: OneToOne FK CASCADE; status: Char pending/submitted/confirmed/failed (db_index); fiscal_sign: Char(128) blank; qr_url: URL blank; payload: JSON; attempts: PositiveSmallInt default 0; submitted_at/confirmed_at: DateTime null.

**Endpoints:** POST `/api/v1/payments/checkout/` (payments:write) → `{payment_id, provider, redirect_url|rpc_payload}`; GET `/api/v1/payments/` + `/{id}/` (payments:read); POST `/{id}/allocate/` (payments:write, body `{allocations:[{invoice,amount}]}`); POST `/{id}/refund/` (payments:write); GET `/reconciliation/?date=` (payments:read); GET `/{id}/receipt/` (payments:read → signed URL); CRUD `/api/v1/payments/provider-configs/` (payments:write, director/accountant only). Public: POST `/api/v1/webhooks/{click|payme|uzum}/<center_slug>/`.

> **Decision (TD-18 exception):** the Payme webhook speaks pure JSON-RPC 2.0 (HTTP 200 always, errors in the `error` member) — Payme's protocol is non-negotiable. Click/Uzum webhook errors use the standard envelope. Record this exception in WORKLOG and flag for `agents/API-CONTRACT.md`.

**Tests required:** Payme golden-fixture suite (one fixture per method, asserting exact error codes incl. -32504/-31001/-31050/-31099 and tiyin math); Click prepare/complete happy path + bad signature; idempotency key reuse; fiscalization task idempotency; reconciliation math; cross-tenant on all endpoints; webhook for center A cannot touch center B's invoices.

**Publish to WORKLOG:** webhook URL shape, `payment_completed`/`payment_failed` signal kwargs for Lane C, mock determinism rules (so Lane F can predict signatures), env keys added (`CLICK_*`, `PAYME_*`, `UZUM_*`, `SOLIQ_*`, all `*_USE_MOCK` default True), TD-18 Payme exception.

---

## Lane C — Notifications (apps/notifications)

**Objective.** The central `dispatch(event)` pipeline: every signal emitted on Days 1–3 becomes SMS/email/push/in-app per user preference, with templates (uz/ru/en), quiet hours, idempotency, and the TD-15 producer side (group_send to `user.{id}`). Implements TASKS §17, TD-13, TD-15 (producer), TD-2. `[OWNER:O-7]` for real FCM — mock-first.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D3-C-1 | Models below + migration; delete `NotificationItem` | `apps/notifications/models.py`, `migrations/`, `admin.py` | Migrate clean; `Notification.dedupe_key` unique | — |
| D3-C-2 | `EventType` enum covering every emitted signal (verify against WORKLOG Days 1–2 + today's lanes) | `apps/notifications/models.py` (TextChoices) | Canonical list (extend, never rename): `attendance.absent`, `attendance.late`, `academics.grades_published`, `assignments.created`, `assignments.due_soon`, `assignments.graded`, `schedule.lesson_reminder`, `auth.new_device_login`, `students.enrollment_changed`, `finance.invoice_issued`, `finance.payment_reminder`, `payments.payment_completed`, `payments.payment_failed`, `cohorts.announcement`, `billing.subscription_past_due`, `billing.subscription_suspended` | WORKLOG audit |
| D3-C-3 | `services.dispatch(*, event_type, recipient_id, context, dedupe_key=None, channels=None)` | `apps/notifications/services.py` | Creates `Notification` (get_or_create on dedupe_key — second call is a no-op returning the existing row); queues `dispatch_notification` Celery task; raises nothing on unknown user (logs + drops) | D3-C-1 |
| D3-C-4 | Receivers for ALL EventTypes' source signals, registered in `NotificationsConfig.ready()` | `apps/notifications/receivers.py`, `apps/notifications/apps.py` | Each Day 1–3 signal connected; absence → guardian recipients resolved via `parents.Guardian`; invoice/payment events → payer + primary guardian; mapping table in module docstring | D3-C-3, signal names from WORKLOG |
| D3-C-5 | Channel fan-out: rewire the stub task | `celery_tasks/notification_tasks.py` (replace TODO body) | Task loads Notification, resolves per-channel preference + quiet hours, calls adapters: SMS `infrastructure.sms.eskiz_client.get_sms_client()`, email `infrastructure.email.email_client.send_email`, push via D3-C-6, in-app = `NotificationDelivery` row + `infrastructure.websocket.channel_layer.group_send(f"user.{id}", {"type": "notification.message", ...})` (TD-15: dispatch is the ONLY group_send producer); each channel result recorded in `NotificationDelivery`; retries max 5 | D3-C-3 |
| D3-C-6 | FCM push client, mock-first | `infrastructure/push/__init__.py`, `infrastructure/push/fcm_client.py` (NEW: `PushClient` ABC, `MockFCMClient`, `FCMClient` via firebase-admin, `get_push_client()`, `FCM_USE_MOCK` default True, `FCM_CREDENTIALS_FILE` env), `config/settings/base.py` (append env keys), `pyproject.toml` (add `firebase-admin`, TD-16) | Mock logs deterministically; real client sends to `Device.push_token` for the user's non-revoked devices. `[OWNER:O-7]` | — |
| D3-C-7 | Template rendering uz/ru/en, safe substitution | `apps/notifications/services.py`, seed default templates in a data migration | Lookup `(event_type, channel, locale)` falling back en→uz; rendering via `string.Template.safe_substitute(context)` — missing vars render literally, **no attribute access, no eval** (Jinja-safe per TASKS §17); user locale from `User.preferred_language` if present else `CenterSettings` default | D3-C-1 |
| D3-C-8 | Preferences with sane defaults + quiet hours | `apps/notifications/services.py`, `selectors.py` | Default matrix: in-app always on; SMS on for attendance.absent, payments.*, finance.*; push on for everything; email on for finance/billing. Quiet hours from `CenterSettings` (default 22:00–07:00 Asia/Tashkent): SMS+push deferred via Celery `eta` to window end; in-app+email send immediately | D3-C-1 |
| D3-C-9 | In-app feed + read receipts + unread count endpoints | `apps/notifications/views.py`, `serializers.py`, `urls.py` | Endpoints below; feed paginated, user sees ONLY own rows (queryset = `request.user`); read-all is one UPDATE | D3-C-1 |
| D3-C-10 | Bulk cohort announcements, rate-limited | `apps/notifications/services.py` (`announce_cohort`), `views.py` | POST creates one Notification per member via chunked Celery tasks with `rate_limit="25/s"`; requires notifications:write; dedupe_key per (announcement_id, user) | D3-C-3 |
| D3-C-11 | Bounce handling | `celery_tasks/notification_tasks.py` | 3 consecutive push failures for a device → clear `Device.push_token` + log delivery as `dead_token`; counted via `NotificationDelivery` history, no users-app schema change | D3-C-5 |

**Models** (tenant schema):

- `Notification` — user: FK users.User CASCADE related_name "notifications"; event_type: Char(64) choices EventType (db_index); title: Char(255); body: Text; data: JSON default dict; dedupe_key: Char(128) **unique** null; read_at: DateTime null (db_index); created_at (db_index). Ordering `-created_at`.
- `NotificationDelivery` — notification: FK CASCADE related_name "deliveries"; channel: Char choices sms/email/push/in_app; status: Char choices sent/failed/skipped_pref/skipped_quiet_hours/dead_token; provider_response: JSON default dict; sent_at: DateTime null; created_at.
- `NotificationPreference` — user: FK CASCADE; event_type: Char(64); channel: Char(16); enabled: Bool. UniqueConstraint(user, event_type, channel). Absent row = default matrix.
- `NotificationTemplate` — event_type: Char(64); channel: Char(16); locale: Char(2) choices uz/ru/en; subject: Char(255) blank; body: Text; is_active: Bool. UniqueConstraint(event_type, channel, locale).

**Endpoints:** GET `/api/v1/notifications/` (notifications:read, own rows only); GET `/api/v1/notifications/unread-count/` → `{count}`; POST `/api/v1/notifications/{id}/read/`; POST `/api/v1/notifications/read-all/`; GET/PUT `/api/v1/notifications/preferences/` (bulk upsert); CRUD `/api/v1/notifications/templates/` (notifications:write, director/IT); POST `/api/v1/notifications/announcements/` (notifications:write).

**Tests required:** dispatch idempotency (same dedupe_key twice → one row, one send); preference matrix parameterized (event × channel × enabled); quiet-hours deferral (freeze time 23:00 → SMS eta = 07:00, in-app immediate); template locale fallback; absence signal end-to-end → guardian gets mock SMS + in-app row; dead-token cleanup after 3 failures; cross-tenant + own-rows-only on feed.

**Publish to WORKLOG:** `dispatch()` exact signature + EventType list (Lane A/B/E call or trigger it), default preference matrix, quiet-hours behavior, `notification.message` group payload shape (Day-4 Lane C `NotificationConsumer` consumes it — this is the TD-15 contract).

---

## Lane D — Audit (apps/audit)

**Objective.** Append-only, signal-driven audit trail per TD-9 with retention, search, and CSV export. Implements TASKS §19, parts of §22 (`cleanup_old_audit_logs`).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D3-D-1 | `AuditLog` model + migration; delete `AuditItem` | `apps/audit/models.py`, `migrations/`, `admin.py` (read-only admin) | Fields below; composite index `(resource_type, resource_id)`, index on `created_at`, `actor` | — |
| D3-D-2 | Receivers for sensitive models, registered in `AuditConfig.ready()` | `apps/audit/receivers.py`, `apps/audit/apps.py` | `post_save`/`post_delete` on (TD-9 list): users.User, users.RoleMembership, finance.Invoice, payments.Payment, academics.Grade, academics.ExamResult, payments.ProviderConfig. Resolve via `django.apps.apps.get_model` inside `ready()` with try/except `LookupError` (lanes merge same day — missing model must not crash). `before` captured via `pre_save` snapshot keyed by instance id. **Encrypted/sensitive fields (national_id, medical_notes, all ProviderConfig credential fields, password) stored as `"***"`** | D3-D-1 |
| D3-D-3 | `audit_log()` helper + auth-flow wiring | `apps/audit/services.py`, `apps/auth/services.py` (additive calls only) | `audit_log(*, actor, action, resource_type="", resource_id="", before=None, after=None, request=None)` extracts ip/ua from request; called on login success/failure, OTP request/verify, logout, token refresh reuse; impersonation call-site documented for D4-E | D3-D-1 |
| D3-D-4 | Append-only API: read-only viewset, cursor pagination, filters | `apps/audit/views.py`, `serializers.py`, `urls.py`, `selectors.py` | `ReadOnlyModelViewSet` using `core.pagination.TimelinePagination` (exists); PUT/PATCH/DELETE/POST → 405; filters: actor, action, resource_type, resource_id, `ts_from`/`ts_to`; `audit:read` per-action perms; selector uses `select_related("actor")` | D3-D-1 |
| D3-D-5 | DB-level append-only note | migration docstring + `docs/` note | Migration includes a comment documenting that app code never updates/deletes AuditLog and that production should additionally `REVOKE UPDATE, DELETE ON audit_auditlog` from the app role (runbook line; actual grant is `[OWNER:O-9]` hosting) | D3-D-1 |
| D3-D-6 | Retention beat task | `celery_tasks/audit_tasks.py` (NEW), `config/celery.py` (append beat entry: weekly) | Deletes rows older than 7y where `resource_type` in {finance.Invoice, payments.Payment, finance.Refund, academics.Grade, academics.ExamResult}; older than 1y otherwise; returns deleted count; idempotent by nature | D3-D-1 |
| D3-D-7 | CSV export endpoint | `apps/audit/views.py` | GET `/api/v1/audit/export/` with same filters; streams CSV; >50,000 matching rows → 400 `validation_error` "narrow your filters"; the export itself is audited via `audit_log(action="export")` | D3-D-4 |

**Model:** `AuditLog` — actor: FK users.User null SET_NULL; actor_repr: Char(255) (snapshot of str(actor)); action: Char(32) choices create/update/delete/login/login_failed/logout/otp_request/otp_verify/impersonate/export (db_index); resource_type: Char(100); resource_id: Char(64); before: JSON null; after: JSON null; ip: GenericIPAddressField null; user_agent: Char(512) blank; created_at: DateTime auto_now_add (db_index). Ordering `-created_at`. No updated_at — rows are immutable.

> **Decision:** `billing.Subscription` (public schema, Lane E) cannot be audited by a tenant-schema `post_save` receiver. Lane E writes subscription audit entries explicitly via `audit_log()` inside `schema_context(center.schema_name)` from its services. Announced in WORKLOG; Lane D exposes the helper, Lane E calls it.

**Endpoints:** GET `/api/v1/audit/` (audit:read, cursor-paginated); GET `/api/v1/audit/{id}/`; GET `/api/v1/audit/export/` (audit:read, CSV).

**Tests required:** save on User produces create+update entries with before/after diff; ProviderConfig audit masks credentials; delete produces entry; PUT/PATCH/DELETE on API → 405 (Lane F duplicates as attack test — coordinate names); retention task deletes correct cohorts (freeze time); login + OTP flows write entries; cross-tenant isolation; cursor pagination stable under inserts.

**Publish to WORKLOG:** `audit_log()` signature (Lanes B/E and D4-E impersonation call it), receiver model list, masking rules, retention classes.

---

## Lane E — Billing / Paywall (apps/billing — NEW shared app)

**Objective.** Platform monetization per TD-8: plans, subscriptions, the 402 paywall middleware, usage metering, plan-limit enforcement, dunning. Public schema only. `[OWNER:O-12]` for real pricing — seed placeholder plans; `[OWNER:O-3][OWNER:O-4]` for owner merchant credentials — mock-first (TD-2).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D3-E-1 | Scaffold `apps/billing` per `docs/adding-an-app.md`; add to **SHARED_APPS** in `config/settings/base.py` (append one line; it must NOT appear in TENANT_APPS) | `apps/billing/{__init__,apps,models,admin,serializers,views,urls,services,selectors}.py`, `apps/billing/{migrations,tests}/__init__.py`, `config/settings/base.py` | `migrate_schemas --shared` creates tables in public schema only; no tenant schema gets billing tables | — |
| D3-E-2 | Models below + migration + seed 3 placeholder plans (`starter`/`standard`/`pro`) in a data migration `[OWNER:O-12]` | `apps/billing/models.py`, `migrations/` | Plans seeded idempotently; constraints below | D3-E-1 |
| D3-E-3 | Auto-subscription on provisioning + trial wiring | `apps/billing/receivers.py`, `apps/billing/apps.py` | `post_save` on `tenancy.Center` (create) → `Subscription(status="trialing", current_period_end=center.trial_ends_at or now+14d)`; existing `Center.on_trial`/`trial_ends_at` (see `apps/tenancy/models.py`) remain the source for trial dates | D3-E-2 |
| D3-E-4 | `SubscriptionGateMiddleware` | `apps/billing/middleware.py` (NEW), `config/settings/base.py` (insert at MIDDLEWARE index 1, immediately after `TenantMainMiddleware`) | On tenant schemas with subscription status `suspended`: respond `402 {"error": {"code": "subscription_required", "detail": ...}}` (TD-18 envelope, JsonResponse). Allowlist prefixes pass through: `/admin/`, `/api/v1/auth/`, `/healthz`, `/api/schema`. Public schema requests: no-op (webhooks unaffected). Subscription looked up by `connection.tenant`, cached per-request, 60s Redis cache to avoid a public-schema query per request | D3-E-2 |
| D3-E-5 | Nightly metering + state flips | `celery_tasks/billing_tasks.py` (NEW), `config/celery.py` (append beat entry: nightly 02:00) | For each active Center: `schema_context` → `UsageSnapshot(students_count, storage_bytes, ai_tokens_used)`. storage_bytes via the D2-E published interface (check WORKLOG; expected `apps.content.selectors.total_storage_bytes()`). AI tokens via D3-E-6 stub. Flips: `trialing` past `current_period_end` + `BILLING_TRIAL_GRACE_DAYS` (settings, default 3) → `suspended`; `active` past period end → `past_due`; `past_due` > `BILLING_DUNNING_DAYS` (default 7) → `suspended`. Snapshot unique per (center, date) — re-run updates, never duplicates | D3-E-2 |
| D3-E-6 | Define AI-usage interface now, stub the number | `apps/ai/selectors.py` (ADDITIVE: one function `tokens_used_current_month() -> int` returning 0 with `# TODO(D4-A): real implementation`) | Function exists and is imported by billing task; D4-A replaces the body. This is a declared cross-lane additive touch — announce in WORKLOG | — |
| D3-E-7 | Plan-limit enforcement hook | `apps/billing/services.py` (`enforce_student_limit()`), `apps/students/services.py` (ONE additive call in the enrollment service) | `enforce_student_limit()` reads `connection.tenant`, compares active student count vs `plan.max_students`, raises `StarforgeError(code="plan_limit_exceeded", status 402)`; enrolling the (max+1)th student fails with that envelope | D3-E-2 |
| D3-E-8 | Platform subscription payment intake | `apps/billing/views.py`, `urls.py`, `config/urls_public.py` (append `path("api/v1/platform/billing/", include("apps.billing.urls"))`) | Reuses `infrastructure/payments` clients with **owner** credentials from env (`PLATFORM_CLICK_SECRET_KEY`, `PLATFORM_PAYME_KEY`, ... appended to settings, mock-first); successful (mock) payment extends `current_period_end` +30d and sets `active`; endpoints restricted to platform staff (`IsAdminUser` — public-schema users exist per TD-3); plain ViewSet, NOT `TenantSafeModelViewSet` (public schema) | D3-B mocks merged |
| D3-E-9 | Dunning notifications | `apps/billing/services.py` | On flip to `past_due`/`suspended`: inside `schema_context`, call `apps.notifications.services.dispatch(event_type="billing.subscription_past_due"| "billing.subscription_suspended", ...)` for users with director role, dedupe key `billing:{center}:{status}:{date}`; plus direct email to `Center.contact_email` (allowed here: billing is platform infrastructure, not a domain app — note in docstring); subscription state changes audited via `audit_log()` in `schema_context` (see Lane D decision) | D3-C, D3-D merged |

**Models** (public schema):

- `Plan` — code: Slug unique; name: Char(100); max_students: PositiveInt; max_branches: PositiveInt; ai_tokens_month: BigInt; storage_gb: PositiveInt; price_uzs: Decimal(18,2); is_active: Bool. (TD-8 field list, verbatim.)
- `Subscription` — center: OneToOne FK tenancy.Center CASCADE related_name "subscription"; plan: FK Plan PROTECT; status: Char choices trialing/active/past_due/suspended (db_index); current_period_start/current_period_end: DateTime; created_at/updated_at. ("Expired" is represented as `suspended`; document in model docstring.)
- `UsageSnapshot` — center: FK Center CASCADE; date: Date; students_count: PositiveInt; storage_bytes: BigInt; ai_tokens_used: BigInt; UniqueConstraint(center, date). Ordering `-date`.

**Endpoints (public schema, platform staff):** GET `/api/v1/platform/billing/plans/`; GET/PATCH `/api/v1/platform/billing/subscriptions/{center_id}/` (PATCH: change plan, set status active/suspended); GET `/api/v1/platform/billing/usage/?center=<id>`; POST `/api/v1/platform/billing/checkout/` → mock payment flow.

**Tests required:** middleware 402 on suspended + allowlist passes + active passes + public schema no-op; trial flip with frozen time; metering snapshot idempotency; student-limit enforcement at boundary (max and max+1); subscription auto-created on Center provisioning; dunning dispatch dedupe.

**Publish to WORKLOG:** middleware position + allowlist, `enforce_student_limit` call site, AI selector stub contract for D4-A (`tokens_used_current_month`), usage interface consumed from D2-E, plan codes for D4-E control center, the TD-9 Subscription-audit pattern used.

---

## Lane F — Day-3 Attack & Cross-Tests

**Objective.** Adversarial verification of everything Lanes A–E shipped today. Implements TASKS §26 (per-day slice), TD-1, TD-4, TD-20. Start after B merges; rebase as C/D/E land. All tests follow `agents/TESTING.md` (two-tenant fixture, factories, query-count helpers).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D3-F-1 | Webhook signature tampering | `apps/payments/tests/test_webhook_attacks.py` | Click: flipped one char in `sign_string` → error `-1`, zero Payment rows. Payme: wrong Basic auth → `-32504`, HTTP 200, JSON-RPC error member. Uzum: bad HMAC → rejected, `WebhookEvent.status="rejected"` recorded | B merged |
| D3-F-2 | Replay protection | same file | Same Payme `CreateTransaction` id twice → identical response, one Payment. Same Click `click_trans_id` complete twice → second recorded `duplicate`, allocation runs once (assert allocation row count) | B |
| D3-F-3 | Wrong-tenant webhook slug | same file | Valid signature for center A posted to `/api/v1/webhooks/payme/<center_b_slug>/` → signature fails against B's `ProviderConfig` (account error in -31050..-31099); no rows in either schema. Nonexistent slug → 404 envelope | B |
| D3-F-4 | Payment idempotency | `apps/payments/tests/test_idempotency_attack.py` | Two concurrent `create_checkout` calls with the same idempotency_key → exactly one Payment (assert via `transaction.on_commit` ordering or sequential second call returns same pk) | B |
| D3-F-5 | Allocation rounding properties | `apps/finance/tests/test_allocation_properties.py` | Parameterized over awkward amounts (e.g. 1,000,000.01 over 3 invoices; 0.01; max-digits boundary): sum(allocations) == payment amount exactly; no invoice over-credited; Decimal not float anywhere (assert types) | A |
| D3-F-6 | Paywall behavior | `apps/billing/tests/test_paywall_attack.py` | Suspended tenant: `/api/v1/students/` → 402 `subscription_required`; `/api/v1/auth/otp/request/`, `/admin/`, `/healthz` reachable; other tenant unaffected; suspended tenant's webhook intake on public schema still works | E |
| D3-F-7 | Audit append-only | `apps/audit/tests/test_append_only_attack.py` | PUT/PATCH/DELETE `/api/v1/audit/{id}/` → 405 even as director and superuser; POST → 405; ORM-level: project code contains no `AuditLog.objects.filter(...).update(` (grep assertion test) | D |
| D3-F-8 | Notification preference matrix + quiet hours | `apps/notifications/tests/test_preference_attack.py` | User who disabled SMS for `payments.payment_completed` gets in-app but no SMS (assert MockEskiz not called); quiet-hours SMS carries eta at window end; dedupe under signal double-fire | C |
| D3-F-9 | Cross-tenant sweep on ALL new endpoints | `apps/*/tests/test_cross_tenant_day3.py` (one per app: finance, payments, notifications, audit) | For every Day-3 endpoint: tenant-A JWT on tenant-B host → 401 `tenant_mismatch` (TD-1); tenant-A data invisible from tenant-B even with director role; uses the TESTING.md two-tenant fixture | A–E |
| D3-F-10 | Payme spec golden suite | `apps/payments/tests/test_payme_spec.py` | One fixture file per JSON-RPC method under `apps/payments/tests/fixtures/payme/`; asserts: amounts in tiyin, `account` passthrough echoed, state transitions 1→2, 1→-1, 2→-2, times in ms, error codes exactly in -31050..-31099 for account errors, `-31001` amount mismatch, `-31003` unknown transaction, `-32601` unknown method | B |

**Publish to WORKLOG:** list of attack vectors covered + any vulnerability found (a found vuln is a P0 handoff to the owning lane same day — file it as `fix(...)` before EOD), final Day-3 coverage number.

---

## Cross-lane integration points

| Producer | Interface | Consumer |
|---|---|---|
| A `apps/finance/services.allocate_payment()` | service call | B (webhook completion), F (rounding tests) |
| A `invoice_issued`, `payment_reminder` signals | django Signal | C receivers |
| A `CashierShift` model | FK target | B `Payment.cashier_shift` |
| B `payment_completed` / `payment_failed` signals | django Signal | C receivers, A (invoice status already updated in B's service before signal) |
| B payment mocks (deterministic) | settings-switched clients | E platform checkout, F attack tests |
| C `dispatch()` + EventType | service call | A/B (indirectly via signals), E dunning (direct call) |
| C `notification.message` group payload | channel layer contract | Day-4 Lane C `NotificationConsumer` |
| D `audit_log()` helper | service call | B (webhook anomalies), E (subscription changes), D4-E impersonation |
| E `enforce_student_limit()` | service call from `apps/students/services.py` | students enrollment (D1-D code) |
| E `apps.ai.selectors.tokens_used_current_month()` stub | function contract | D4-A replaces body, D3-E-5 calls it |

Merge order recap: **A → B → C → D → E → F**. After each merge, the next lane rebases. Migration number collisions: later merger renumbers (ROADMAP §2.3).

---

## EOD gate — all boxes checked before Day 3 closes

- [ ] `uv run ruff check . && uv run ruff format --check .` — clean
- [ ] `uv run mypy apps core infrastructure config` — clean
- [ ] `uv run pytest -q --cov=apps --cov=core --cov-fail-under=80` — green (TD-20: floor is 80 today); CI green on master
- [ ] `uv run python manage.py makemigrations --check --dry-run` — no drift; fresh `migrate_schemas --shared` + tenant provisioning succeeds
- [ ] OpenAPI schema job green; every new endpoint has `@extend_schema` with examples
- [ ] **Demo script** (run against seeded `demo` tenant, all mocks):
  1. Enroll a student → invoice auto-issued (`invoice_issued` fires) → parent gets in-app notification + mock SMS.
  2. POST mock Payme `CheckPerformTransaction` → `CreateTransaction` → `PerformTransaction` to `/api/v1/webhooks/payme/demo/` → Payment completed, allocation recorded, invoice `paid`, `FiscalReceipt` confirmed (mock), `payments.payment_completed` notification delivered, AuditLog rows for Invoice + Payment visible at `/api/v1/audit/`.
  3. Replay the same `PerformTransaction` → no second allocation.
  4. GET `/api/v1/payments/reconciliation/?date=today` → totals match.
  5. Generate statement PDF → signed URL downloads.
  6. Suspend `demo` subscription via `/api/v1/platform/billing/subscriptions/{id}/` → tenant API returns 402; auth + healthz still reachable; reactivate → 200 again.
  7. Attempt PATCH on an audit row → 405.
- [ ] TASKS.md ticked: §15 (all), §16 (all), §17 (all except deferred email open/click tracking — mark `[~]` design note), §19 (all), §22 items `late_payment_reminders` + `cleanup_old_audit_logs`. Items still blocked on owner creds marked `[~] BLOCKED(O-x)` per ROADMAP §2.4
- [ ] WORKLOG entries appended for all six lanes, each publishing the interfaces listed in its "Publish to WORKLOG" block — Day-4 lanes (AI, reports, realtime, control center) read these tomorrow morning
- [ ] No secrets committed; all new env keys documented in WORKLOG with `*_USE_MOCK` defaults True
