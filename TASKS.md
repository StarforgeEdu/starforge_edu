# starforge_edu — backend tasks (exhaustive)

Every feature, every detail, in dependency order. Check items off as you ship them.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done

---

## 0. Bootstrap (do first thing next session)

- [~] `docker compose -f docker/docker-compose.yml up -d postgres redis minio` (owner is setting up Postgres)
- [~] Wait for `pg_isready` and `redis-cli ping` to succeed (pending owner DB)
- [~] Create the `starforge` database if absent (pending owner DB)
- [x] `uv run python manage.py makemigrations` — generated real `0001_initial.py` per app (26 migration files)
- [x] Inspect each generated migration before committing (spot-checked `users`/`org`: db_constraint=False FKs + dependency ordering correct)
- [ ] Commit migrations as a single commit: `chore(migrations): initial migration graph for v1 apps` (will commit when asked)
- [~] `uv run python manage.py migrate_schemas --shared` — public schema (pending owner DB)
- [~] `uv run python scripts/seed_dev.py` (seed extended: branch/dept/2 teachers/cohort/5 students/2 parents; pending owner DB to run)
- [~] Verify `http://demo.localhost:8000/admin/` loads and login works (pending owner DB)
- [~] Verify `http://demo.localhost:8000/api/schema/swagger-ui/` renders (pending owner DB)
- [~] Hit `POST /api/v1/auth/login/ {"username","password"}`, get `{access, refresh}` back (AUTH PIVOT 2026-06-11: login = username+password; test written; pending owner DB)
- [~] Hit `POST .../auth/password/reset/request/` + `confirm/` — OTP now serves password reset only (tests written; pending owner DB)
- [~] Hit `GET /api/v1/users/me/` with Bearer, get 200 (test written; pending owner DB)
- [~] Verify tenant isolation: token from `demo` must NOT work against another tenant's hostname (test GREEN-by-construction, TD-1; pending owner DB to run)

---

## 1. Tooling, CI, infra polish

- [~] Pre-commit: `uv run pre-commit install` to wire local hooks (owner runs locally)
- [~] CI: push to GitHub, confirm jobs green (ci.yml updated; pending push)
- [x] Add `coverage` reporting to the test job (`pytest --cov=apps --cov=core --cov-fail-under=70`)
- [x] Add a `dependabot.yml` for weekly Python + GitHub Actions updates
- [~] Add a CODEOWNERS file once the team is more than one person (deferred — single dev)
- [x] Add a `Makefile` with `up`/`migrate`/`seed`/`test`/`lint`/`schema`/`makemigrations`
- [x] Configure ruff to fail on warnings in CI (verified — `ruff check` clean)
- [x] Configure mypy to run cleanly (now FULLY clean — 0 errors across 260 files)
- [~] Wire `drf-spectacular` schema diff in CI (DEFERRED to D5-D per plan)
- [x] Add Sentry error tracking (config-only, guarded; no DSN committed)
- [x] Add structured JSON logging for prod settings (`JsonFormatter`, prod only)
- [x] Add request ID middleware (`X-Request-ID` echoed back, surfaced in logs)
- [x] Add health check endpoints: `/healthz/live` + `/healthz/ready` (DB + Redis)
- [~] Add Prometheus metrics endpoint (DEFERRED to D5-A per plan)
- [~] Document runbook for rotating SECRET_KEY and ESKIZ credentials (DEFERRED to D5-E)

---

## 2. Tenancy (apps/tenancy + apps/org)

### Center lifecycle

- [x] `provision_center` service: validate slug is Postgres-safe (regex `^[a-z][a-z0-9_]{0,62}$`)
- [x] Reject duplicate slug with a clear error (`slug_taken`, pre-check not IntegrityError)
- [x] Reject reserved slugs: `public`, `admin`, `www`, `api`, `static`, `media` (`slug_reserved`)
- [x] On Center delete, refuse if tenant has > 0 users (require explicit `force=True`)
- [x] Center deactivation flow: `InactiveTenantMiddleware` → 503 `center_inactive`
- [x] Trial expiration: `deactivate_expired_trials` beat task (hourly, idempotent)
- [x] Center "soft delete" with archival schema rename + `archive_center` management command

### Domain management

- [x] Multiple domains per Center — `set-primary` platform endpoint (atomic)
- [~] Domain ownership verification: TXT record check (stub `verify_domain_txt` mock-pass, O-8)
- [ ] Wildcard fallback: `*.starforge.uz` resolves to apex with marketing site (O-8)

### Branch / Department

- [x] Branch schema: working hours, holidays, room list
- [x] Department: budget, head-of-department FK (+ TeacherProfile validation)
- [x] Branch ↔ Department ↔ User membership table (`RoleMembership`, now consumed)
- [~] Move/transfer student between branches: `record_transfer` history (full cascade is D2+)
- [x] Soft-delete a Branch: refuse if it has active students; allow if archived

### Public-schema admin

- [x] Restrict `/admin/` on apex domain to platform staff only (TD-3 public users + IsAdminUser)
- [ ] Tenant impersonation tool (DEFERRED to D4-E control center)
- [ ] Tenant usage dashboard (DEFERRED to D4-E control center)

---

## 3. Users + Auth (apps/users + apps/auth)

> **AUTH PIVOT (owner decision, 2026-06-11):** login is **username + password**
> (`POST /api/v1/auth/login/`); `User.username` is the unique identity
> (auto-generated for staff-created accounts). OTP items below are repurposed —
> they now power **password reset** (`/auth/password/reset/{request,confirm}/`)
> and future contact verification, never login. "Phone OR email login" items
> apply to `/admin/` sessions only.

### User model

- [ ] Add `avatar` ImageField (S3-backed via django-storages)
- [~] Add `birthdate`, `gender`, `national_id` (birthdate+gender DONE; national_id via EncryptedCharField deferred)
- [ ] Add `address` (separate model: country/region/city/street/postal)
- [x] Add `preferred_language` (one of uz/en/ru)
- [ ] Add `notification_preferences` per channel × event type (D3 notifications)
- [x] Validate phone in E.164 (`core.validators.normalize_phone`)
- [~] Validate email uniqueness case-insensitively (lowered on create; DB citext deferred)
- [x] Allow either phone OR email login (BOTH-for-high-risk is TBD)
- [ ] User merge tool: combine two users with the same person behind them (admin-only)
- [ ] User deactivation vs deletion (GDPR-style erasure with audit retention)

### OTP

- [ ] OTP code length configurable per channel (SMS=6, email=8?)
- [x] OTP cooldown: last OTP < 60s ago → 429 (CenterSettings.otp_cooldown_seconds)
- [x] OTP attempt limit: 5 wrong codes → throttled (increment now persists before raise)
- [x] OTP audit log: `otp_requested/verified/failed` signals with ip + user_agent (log-only; AuditLog D3)
- [x] Detect OTP enumeration: per-IP distinct-identifier cap per hour
- [ ] OTP via WhatsApp — separate channel
- [ ] OTP voice call fallback (Eskiz supports this)
- [~] OTP for email needs a different template (HTML + plaintext) — D3 notifications
- [x] OTP cleanup: `purge_expired_otps` registered daily in `CELERY_BEAT_SCHEDULE`

### JWT

- [x] Issue JWT with extra claims: `schema`, `tv`, `roles[]` (TD-1, on access + refresh)
- [x] Token versioning: bump `token_version` to invalidate all live tokens
- [x] Refresh token reuse detection: blacklisted refresh reused → revoke ALL + bump tv (`refresh_reused`)
- [~] Device-bound refresh: device registered on verify; refresh-time device validation deferred
- [x] Logout-everywhere endpoint: `POST /api/v1/auth/logout-all/`
- [ ] JWT revocation list cleanup: scheduled task to drop blacklisted refreshes after expiry

### Devices

- [x] Device registration on login (auto-create from User-Agent + client device_id)
- [x] Device list endpoint: `GET /api/v1/users/devices/`
- [x] Device revocation: `DELETE /api/v1/users/devices/{id}/` (soft, sets `revoked_at`)
- [x] Push token registration per device (store-only, O-7 for real FCM/APNs send)
- [ ] Detect impossible travel: flag same user from far-apart IPs within minutes

### Sessions / admin

- [x] Force re-login when password changes (`set_user_password` bumps tv)
- [x] Force re-login when role membership changes (RoleMembership receivers bump tv)
- [x] "Last seen at" updated on every authenticated request (throttled, in authenticator)
- [ ] "Login from new device" notification (push + email) (D3)

### Permissions

- [x] Wire ROLE_PERMISSION_MATRIX into every ViewSet (`resource`+`required_perms`, flat `required_perm` removed)
- [x] Object-level scoping: viewsets set `object_scope` where branch/department-scoped
- [x] Permission cache: memoize role memberships per request (one query)
- [ ] Add `RoleMembership` admin UI with bulk grant / bulk revoke
- [~] Audit every permission grant (tv-bump receivers exist; AuditLog row in D3)
- [x] Permission test matrix: parameterized pytest over (role, endpoint, verb)

---

## 4. Org structure (apps/org)

- [x] Branch CRUD with permission gates (only director / IT)
- [x] Department CRUD scoped to a Branch
- [x] Branch operating hours by weekday (bulk-replace endpoint)
- [x] Branch holidays (per-branch override; national-holiday seeding is D2)
- [x] Department head assignment (FK to User, validates TeacherProfile)
- [~] Department budget vs spent rollup (budget field done; spent-from-finance is D3)
- [x] Branch transfer history (`BranchTransfer` + `record_transfer`)
- [x] Branch capacity tracking (max students, max teachers — soft caps + `capacity_status`)
- [x] Room model under Branch: name, capacity, equipment (availability windows D2)

---

## 5. Students (apps/students)

- [x] Replace placeholder `StudentItem` with real `StudentProfile(OneToOne→User)`
- [x] Fields: enrollment_date, academic_level, current_cohort, guardian links, medical_notes (encrypted), emergency_contacts (JSON)
- [x] Student photo (S3 via django-storages, ImageField)
- [x] Student ID generation (auto, per-Center pattern — `DEMO-2026-00042`)
- [x] Enrollment workflow state machine (lead → … → graduated/withdrawn, re-enroll)
- [x] Drop / re-enroll history with reason codes (`EnrollmentEvent`)
- [x] Bulk import from CSV (stdlib `csv` not pandas; savepoint-per-row)
- [~] Student dashboard endpoint (skeleton; full aggregation D3)
- [x] Birthday list endpoint (filter by branch/cohort)
- [x] Student search on name + phone + ID (icontains; FTS deferred to D5)

---

## 6. Parents (apps/parents)

- [x] Replace placeholder with real `ParentProfile(OneToOne→User)`
- [x] `Guardian` link model: parent → student, with relationship type
- [x] Primary guardian flag (one per student — conditional UniqueConstraint + service guard)
- [x] Custody / visitation rules (`custody_notes` field)
- [x] Multiple students per parent (siblings) — `GET /parents/{id}/students/`
- [x] Parent → student visibility scope (selector scoping, read_own_children)
- [~] Parent app endpoints: dashboard per linked student (students list; full dashboard D3)
- [x] Pickup authorization list (separate from Guardian)
- [ ] Parent satisfaction survey (defer)

---

## 7. Teachers (apps/teachers)

- [x] Replace placeholder with real `TeacherProfile(OneToOne→User)`
- [x] Fields: hire_date, subjects[], qualifications, salary_type+rate, department FK
- [ ] Teacher availability calendar (weekly windows) (D2)
- [x] Substitute teacher pool (`is_substitute` flag + filter)
- [ ] Teacher load report (D2)
- [ ] Performance reviews (lifecycle)
- [ ] Teacher payroll inputs (push to `apps.finance` once wired)

---

## 8. Cohorts (apps/cohorts) — class groups

- [x] Replace placeholder with real `Cohort` model
- [x] Fields: name, branch FK, department FK, level, start/end_date, capacity, primary_teacher FK
- [x] Cohort membership: students with start/end dates (one-active constraint)
- [x] Co-teacher assignments (`CohortTeacher`)
- [x] Cohort rooms (`default_room` FK)
- [x] Move student between cohorts mid-term (history preserved, `cohort_member_moved` signal)
- [x] Cohort archive at end of term (read-only; writes → `cohort_archived`)
- [ ] Cohort messaging: bulk send (via notifications, D3)

---

## 9. Schedule (apps/schedule)

- [x] `Term`/`TimeSlot`/`RecurrenceRule`/`Lesson` models (Room/Holiday already in org, D1-F)
- [x] Recurring lessons (TD-12 materialized occurrences via dateutil rrule)
- [x] One-off lesson edits (cancel one occurrence, move one occurrence → detaches)
- [x] Room booking conflict detection (service 409 + DB exclusion constraint)
- [x] Teacher conflict detection (service 409 + DB exclusion constraint)
- [x] Cohort conflict detection (service 409 + DB exclusion constraint)
- [~] Holiday skip on materialize (per-branch `org.BranchHoliday`); national-holiday seeding deferred
- [x] iCalendar feed per user (signed token URL, tenant-bound)
- [~] Reminder 30 min before lesson: `lesson_reminder_due` signal emitted (push wiring D3-C)
- [x] Bulk reschedule (whole-week shift, all-or-nothing)
- [x] Term boundaries auto-archive lessons (`archive_completed_terms` beat task)

---

## 10. Attendance (apps/attendance)

- [x] Replace placeholder with real `AttendanceRecord` model: student, lesson, status (present/absent/late/excused), marked_by, marked_at, note (+ arrived_at, auto_marked; unique(student,lesson)) — `attendance/0002`
- [x] Mark-attendance endpoint scoped to a Lesson (teacher only) — `POST /api/v1/attendance/lessons/{id}/mark/`; director/head_of_dept bypass the teacher check
- [x] Bulk mark by cohort — the mark payload is a list of entries (post every cohort student in one call)
- [x] Attendance summary per student per term (% present) — `GET /api/v1/attendance/summary/?student=&term=`
- [x] Auto-mark "absent" 30 min after lesson start if no record exists (Celery) — `mark_absent_after_lesson` (knob `auto_absent_after_minutes`, idempotent)
- [x] Late threshold configurable per Center (default 10 min) — `late_threshold_minutes` drives auto-late from `arrived_at`
- [x] Notify guardian on absence — `student_marked_absent` signal emitted (manual + auto); SMS/in-app **dispatch is D3-C** (emit-only today)
- [x] Attendance correction window (24h to amend; after that requires director approval) — `attendance_correction_window_hours`; past-window edits 403 `correction_window_expired` unless director
- [x] Attendance export per cohort per term (CSV) — `GET /api/v1/attendance/export/?cohort=&term=` streaming `text/csv` (**PDF deferred** to a TD-14 lane)
- [x] Attendance dashboard per cohort per teacher — `GET /api/v1/attendance/cohorts/{id}/dashboard/` (single aggregate query, staff/teaching-teacher only)

---

## 11. Academics (apps/academics)

- [x] Replace placeholder with `Subject`, `Exam`, `ExamResult`, `Grade`, `Transcript` models — `academics/0002`
- [x] Grading scheme (per-Center: letter A–F, GPA 0–4, percentage 0–100) — `apps/academics/grading.py`, knob-driven `value_display`
- [x] Exam types: midterm, final, quiz, project, oral
- [ ] Exam generation (AI-assisted via apps.ai — gated) — **deferred to D4-A §18** (out of Day-2 scope, per Lane C objective)
- [x] Grade entry per cohort per exam — `POST /api/v1/academics/exams/{id}/results/`
- [x] Bulk grade entry by CSV — `POST .../results/import-csv/` (all-or-nothing; 422 lists bad row numbers)
- [x] Grade audit (who changed, when, old value) — `grade_changed` signal (old/new on overwrite); **D3-D audit** consumes
- [x] Auto-calculate term grade from weighted exam results — `compute_term_grade` / `recompute_cohort_term` (published exams only)
- [x] Generate transcript PDF per student — `POST .../transcripts/` → Celery `generate_transcript_pdf` (weasyprint → S3, TD-14)
- [x] Honor roll / academic-warning detection (configurable thresholds) — `honor_roll_min`/`academic_warning_max` knobs + endpoints
- [x] Parent visibility: only student's grades, only after publication — `scoped_grades` (is_published gate + guardian scoping)

---

## 12. Assignments / homework (apps/assignments)

- [x] Replace placeholder with `Assignment`, `Submission`, `Grade` models (`SubmissionGrade`) — `assignments/0002`
- [x] Teacher creates assignment for a cohort with due date + attachments — CRUD + `/publish/`
- [x] Student submits with attachments (S3) or text body — `POST /assignments/{id}/submissions/` (presigned via `/upload-url/`)
- [x] Late submission flag with configurable grace period — `assignment_grace_minutes` (is_late vs `due_at + grace`)
- [x] Plagiarism check stub (defer real integration) — `check_submission()` → typed `PlagiarismResult(not_implemented)`
- [ ] AI-assisted feedback (apps.ai) — signal `ai_feedback_requested` emitted + `request-ai-feedback/` endpoint; **real AI is D4-A §18**
- [x] Resubmit allowed up to N times — `assignment_max_resubmits` knob (per-assignment `max_resubmits` override)
- [x] Grade rubric per assignment — `rubric` JSON + `grade_submission` validates criteria & Σ max_points ≤ max_score
- [x] Assignment notifications: created, due-soon, graded — signals `assignment_published`/`assignment_due_soon`/`submission_graded` (emit-only; **dispatch D3-C**)

---

## 13. Content (apps/content)

- [x] Replace placeholder with `LessonFile`, `Folder`, `ContentLibrary` models (+ Course/Module/ContentLesson/FileView) — `content/0002`
- [x] Hierarchy: Subject → Course → Module → Lesson → File — `ContentLibrary` → Course → Module → ContentLesson → LessonFile
- [x] File upload via signed S3 URL — `POST /content/upload-url/` → direct PUT → `/files/{id}/confirm/`
- [x] File-type allowlist (PDF, MP4, PPTX, DOCX, MP3, common image formats) — extension + declared content-type checked
- [x] File size cap (configurable per Center, default 200MB) — `max_upload_mb` knob
- [x] libmagic content-type validation on upload — `validate_uploaded_file` sniffs first 8KB (lazy `magic`)
- [ ] Antivirus scan stub (defer ClamAV integration) — **deferred** (magic sniff is the validation gate today)
- [x] Versioning per file — `POST /files/{id}/new-version/` (links `previous_version`, `version+1`)
- [x] Visibility scoping: department / cohort / role — `scoped_files`/`scoped_libraries` (tenant/department/cohort/role)
- [x] Watch / view tracking (who opened what, when) — `FileView` rows + `track-view/`
- [x] Download counter — `F()`-incremented `download_count` on `download-url/`
- [ ] AI summary per file (gated by tenant AI budget) — **deferred to D4-A §18**

---

## 14. Printing (apps/printing) — server side only

- [x] Replace placeholder with `PrintJob`, `Printer`, `BranchAgent` models
- [x] PrintJob fields: status (queued/picked/printing/done/failed), source (assignment/transcript/report/receipt), payload (S3 key), pages, copies, color, duplex, branch_id, agent_id, requested_by
- [x] Printer registration per Branch (name, model, capabilities)
- [x] Branch agent auth: hashed API token bound to a Branch (`Authorization: Agent <raw>`)
- [x] Job claim endpoint for the agent: `POST /api/v1/printing/agent/claim/` returns next queued job (select_for_update skip_locked)
- [x] Job status update endpoint for the agent (`POST /api/v1/printing/agent/jobs/<id>/status/`, transition matrix)
- [x] Job retry policy on failure (max 3, exponential backoff via next_attempt_at)
- [x] Print quotas per cohort per term (paper saving) — CenterSettings.print_quota_pages_per_cohort_term (0 = unlimited)
- [x] Print job audit (who printed what, when, how many pages) — print.job_created/done/failed
- [x] **NOTE:** the actual CUPS-talking branch agent is a separate repo. Don't add CUPS code here.

---

## 15. Finance (apps/finance)

- [ ] Replace placeholder with `Invoice`, `InvoiceLine`, `Discount`, `Refund`, `CashierShift` models
- [ ] Invoice issuance per student per term (auto on enrollment)
- [ ] Tuition fee schedules per Center / per Cohort
- [ ] Sibling discount, scholarship, payment plan
- [ ] Payment allocation (one payment may cover multiple invoices)
- [ ] Cashier shift open/close with cash count
- [ ] Daily cashier report
- [ ] Outstanding balance per student
- [ ] Late-payment reminder schedule (Celery beat → notification dispatch)
- [ ] Currency: UZS as primary, USD secondary; per-Center config
- [ ] FX rate snapshot per invoice (so historical totals don't drift)
- [ ] Parent statement of account endpoint (PDF)

---

## 16. Payments (apps/payments)

- [ ] Replace placeholder with `Payment`, `PaymentAttempt`, `WebhookEvent` models
- [ ] Provider: Click — implement create_invoice, prepare, complete, signature verify
- [ ] Provider: Payme — implement JSON-RPC handlers (CheckPerformTransaction, CreateTransaction, PerformTransaction, CancelTransaction, CheckTransaction, GetStatement)
- [ ] Provider: Uzum — implement webhooks
- [ ] Idempotency key on every Payment creation
- [ ] Webhook signature verification (provider-specific)
- [ ] Webhook replay protection (store nonce, reject duplicates)
- [ ] Payment → Invoice allocation (auto when amounts match exactly; manual review otherwise)
- [ ] Refund flow (provider-specific; store refund attempts with state machine)
- [ ] Reconciliation report (daily): payments received vs invoices marked paid
- [ ] Payment receipt PDF generation
- [ ] Failed payment notification

---

## 17. Notifications (apps/notifications)

- [ ] Replace placeholder with `Notification`, `NotificationPreference`, `NotificationTemplate` models
- [ ] Channels: SMS (Eskiz), Email, Push (FCM/APNs), In-app
- [ ] Event types enum (long list; one per event the platform emits)
- [ ] Per-user × per-event-type × per-channel preferences (default sane)
- [ ] `dispatch(event)` central function — every other app calls this, never channels directly
- [ ] Templates with uz/en/ru variants
- [ ] Template variable substitution (Jinja-safe)
- [ ] Idempotency on dispatch (don't send same notification twice)
- [ ] Quiet hours per user (no SMS at 11pm–7am)
- [ ] Bulk notification (cohort-wide announcements) with rate limiting
- [ ] Notification history per user (in-app feed)
- [ ] Read receipts for in-app notifications
- [ ] Email open / click tracking (defer; design only)
- [ ] Bounce handling per channel (mark device push token dead after N failures)

---

## 18. AI (apps/ai)

- [ ] Replace placeholder with `TenantAIBudget`, `AIRequest`, `AIPrompt` models
- [ ] Per-Center daily/monthly token budget (defaults wired in settings)
- [ ] Pre-flight budget check before queueing any AI Celery task
- [ ] AI calls are Celery-only — no synchronous calls from request handlers
- [ ] Prompt caching via Anthropic + Redis (already wired in infrastructure/ai/anthropic_client.py)
- [ ] Use cases v1: assignment feedback, exam question generation, content summarization
- [ ] Per-feature cost cap (e.g. exam-gen costs N tokens — block if budget < N)
- [ ] AI usage report per Center per month
- [ ] Anonymize student data before sending to AI (no real names if avoidable)
- [ ] Redact PII from prompts (regex pass + LLM-based as fallback)
- [ ] Prompt registry (versioned, one place per use case)
- [ ] A/B prompt experiments (defer)

---

## 19. Audit (apps/audit)

- [ ] Replace placeholder with real `AuditLog` model: actor, action, resource_type, resource_id, before, after, ip, ua, ts
- [ ] Auto-record on save/delete via Django signals for sensitive models (User, RoleMembership, Invoice, Payment, Grade)
- [ ] Manual audit_log() helper for non-model events (login, logout, OTP request)
- [ ] Audit log is append-only (no DELETE permission)
- [ ] Retention policy: 7 years for finance/grades, 1 year for everything else
- [ ] Audit search UI (filter by actor, resource, time range)
- [ ] Audit export (CSV) for compliance requests

---

## 20. Reports (apps/reports)

- [x] Replace placeholder with `Report`, `ReportRun`, `ReportSchedule` models
- [x] Report library: enrollment, attendance, grades, finance, AI usage, storage usage
- [x] One-shot generation via Celery (writes to S3, signed URL — delivered via notifications.dispatch, not email directly)
- [x] Scheduled reports (weekly/monthly) via django-celery-beat (`run_due_report_schedules`, last_run_at guard)
- [x] Per-role visibility: directors see all, accountants see finance, teachers see their cohorts (selector-scoped)
- [x] PDF + Excel exports (weasyprint/openpyxl LAZY; render tests skip when absent)
- [x] Cross-tenant analytics (platform admin only) — `nightly_platform_aggregation` → billing.UsageSnapshot

---

## 21. Channels / Realtime

- [ ] Replace demo PingConsumer with real consumers per app:
  - [ ] `attendance` — live attendance updates per cohort
  - [ ] `notifications` — in-app notification stream per user
  - [ ] `chat` (defer) — direct messaging between teacher and parent
- [ ] Tenant resolution from hostname (already wired in middleware)
- [ ] JWT auth on connect (already wired)
- [ ] Per-user group: `user.{id}` for direct push
- [ ] Per-cohort group: `cohort.{id}`
- [ ] Per-branch group: `branch.{id}`
- [ ] Disconnect cleanup (remove from groups)
- [ ] Heartbeat / ping-pong every 30s
- [ ] Reconnect-with-backoff guidance for clients (document)

---

## 22. Celery / background jobs

- [ ] Periodic tasks via django-celery-beat (defined in admin or in code via `setup_periodic_tasks`):
  - [ ] `purge_expired_otps` daily
  - [x] `mark_absent_after_lesson` every 15 min — fan-out per active Center (`celery_tasks/attendance_tasks.py`)
  - [ ] `late_payment_reminders` daily
  - [ ] `nightly_aggregations` for cross-tenant analytics
  - [ ] `archive_completed_terms` weekly
  - [ ] `cleanup_old_audit_logs` weekly (per retention policy)
- [ ] Idempotency: every task that calls Eskiz/Click/Payme/Uzum/Anthropic must store an idempotency key on the source row
- [ ] Retry policy: exponential backoff, max 3 by default, tunable per task
- [ ] Dead-letter queue for tasks that exhaust retries
- [ ] Task duration metrics
- [ ] Worker autoscaling (defer; design)

---

## 23. Storage

- [x] MinIO bucket creation in `seed_dev.py` — `bootstrap_dev_storage()` (idempotent `create_bucket`)
- [x] Bucket lifecycle: expire objects under `tmp/` after 7 days — `_TMP_LIFECYCLE` (see schema-first-key note in WORKLOG)
- [x] Signed upload flow: `POST /api/v1/content/upload-url/` → direct S3 PUT → `/files/{id}/confirm/`
- [x] Signed download URL endpoints with short TTLs — `/files/{id}/download-url/` (TTL 300)
- [x] Per-tenant bucket prefix: `{schema_name}/...` so a shared bucket still isolates data
- [x] CORS config for direct browser uploads — `_DEV_CORS` in `bootstrap_dev_storage`
- [x] Content-type allowlist enforced on upload-url issuance — `request_upload` (extension + declared type)
- [x] File metadata extraction on upload-complete callback (libmagic) — `validate_uploaded_file` (head_object + magic sniff)
- [x] Image thumbnail generation (Pillow, async via Celery) — `generate_thumbnail` (320px, `.../thumb.jpg`)
- [ ] Video transcoding (defer; pluggable) — **deferred** per spec
- [x] Storage quota per Center — `storage_quota_gb` knob + `storage_used_bytes()` enforcement

---

## 24. Internationalization

- [ ] Mark every user-facing string with `gettext_lazy`
- [ ] `python manage.py makemessages -l uz -l en -l ru`
- [ ] Translate strings (uz first)
- [ ] Compile `.mo` files in CI
- [ ] SMS templates per language
- [ ] Email templates per language
- [ ] Locale switcher in user profile
- [ ] Locale auto-detect from `Accept-Language` (already via LocaleMiddleware)
- [ ] Number / date / currency formatting per locale

---

## 25. Security hardening

- [ ] CSRF tokens for cookie-based endpoints (admin only)
- [ ] CORS allowlist tightened in production (no `CORS_ALLOW_ALL_ORIGINS`)
- [ ] HSTS preload registration after launch
- [ ] Rate limit on auth endpoints (already done via DRF throttle classes)
- [ ] Rate limit on AI endpoints (token-bucket per Center)
- [ ] Brute-force lockout on `/admin/` login (django-axes or equivalent)
- [ ] CSP headers via django-csp
- [ ] X-Frame-Options DENY (already set)
- [ ] X-Content-Type-Options nosniff
- [ ] Secrets rotation runbook
- [ ] Field-level encryption for `national_id`, `medical_notes` (django-cryptography or pgcrypto)
- [ ] Audit log for every admin action
- [ ] Penetration testing scope document

---

## 26. Tests

- [x] **Tenant isolation invariant** — JWT in tenant A fails on tenant B (`tests/test_tenant_isolation.py`)
- [x] OTP request → verify happy path (MockEskiz outbox)
- [~] OTP throttle: 4th request in a minute returns 429 (throttles wired; explicit test deferred)
- [x] OTP wrong code rejected (5x cap now persists; explicit 5x test deferred)
- [x] Refresh token rotation: old refresh blacklisted after rotation
- [x] Refresh reuse detection: blacklisted refresh reused → all revoked
- [x] User can log in with phone OR email
- [x] Permission matrix: parameterized over (role, endpoint, verb) — 52 cases (Day-1 + Day-2 resources) + fail-closed
- [x] Object-scoped permission: teacher branch A vs branch B → 403, director bypass → 200 (`tests/test_object_scope.py`, D2-F)
- [x] Channels: anonymous WS connection rejected (4401)
- [~] Channels: authenticated WS receives "hello" (anonymous-reject done; authed test D4-C)
- [x] Celery task isolation: eager `purge_expired_otps` under schema_context
- [x] Migration: `migrate_schemas --shared` on fresh DB — `pytest --create-db` full run applies every Day-2 migration (incl. btree_gist) green
- [x] Migration: creating a new Center auto-runs tenant migrations — conftest provisions tenant_a/tenant_b on a fresh DB (RUN)
- [x] OpenAPI schema generation succeeds (CI job exists) — **0 errors**; `core.schema` registers the JWT auth extension
- [x] Coverage threshold ≥ 70% — **MEASURED 88.77%** on real Postgres (`pytest --cov`)
- [x] Day-2 cross-tenant isolation per resource (schedule/attendance/academics/assignments/content) — per-lane tests
- [x] Day-2 query-count budgets on every list endpoint (≤8, constant w.r.t. row count)
- [x] Conflict-detection property tests: overlap cases × room/teacher/cohort × (service 409 + raw-ORM IntegrityError) — `apps/schedule/tests/test_conflict_properties.py`
- [x] Layering guard: zero sms/email/ai adapter imports in Day-2 apps (`tests/test_layering.py`)
- [x] Shared in-memory S3 stub for the upload flow (`tests/storage_stub.py`, `s3_stub` fixture) + live-MinIO marker

**Note (Day 2):** the suite now **runs on real Postgres** — 338 passing, 2 skipped (weasyprint /
libmagic native libs absent on the Windows dev box; CI/Linux runs them), 88.77% coverage.

---

## 27. Frontends (out of v1 scope, but tracked)

- [ ] React admin scaffold (Vite + TanStack Query + drf-spectacular-generated TS client)
- [ ] Flutter mobile scaffold (Dio + drf-spectacular-generated Dart client)
- [ ] Auth flow on both
- [ ] Tenant subdomain detection (web)
- [ ] Tenant header injection (mobile)

---

## 28. Branch print agent (separate repo, tracked here)

- [ ] Bootstrap a separate `starforge-print-agent` repo (Go preferred; Python if forced)
- [ ] Long-poll or WS connection back to server
- [ ] CUPS integration (`pkg.go.dev/github.com/OpenPrinting/goipp` or python-cups)
- [ ] Job claim → print → status update loop
- [ ] Auto-update mechanism (the agent is on a printer-room PC, not a server)
- [ ] Telemetry: which printer printed what, when, errors
- [ ] One-line installer (Linux/Windows)

---

## 29. Deployment (post-v1)

- [ ] Pick a hosting target (Hetzner / DO / AWS / on-prem)
- [ ] Managed Postgres setup with daily backups
- [ ] Managed Redis (or self-hosted with persistence)
- [ ] Object storage (S3 / Backblaze / MinIO cluster)
- [ ] Reverse proxy with wildcard TLS (Caddy or Traefik with cert-manager DNS-01)
- [ ] Container registry
- [ ] CI → CD pipeline (deploy on `main` push)
- [ ] Blue/green or rolling deploy strategy
- [ ] DB migration safety (check for backwards-compatible changes; block deploys that aren't)
- [ ] Observability: logs aggregation, metrics, traces
- [ ] Alerting on error rate, latency, queue depth
- [ ] Incident response runbook
- [ ] Disaster recovery: RPO/RTO defined, restore drill quarterly

---

## 30. Documentation

- [ ] Per-app README explaining its domain and key models
- [ ] API user guide (separate from the auto-generated OpenAPI)
- [ ] Architecture decision records (ADR) for the big choices already made:
  - [ ] ADR-001: schema-per-tenant via django-tenants
  - [ ] ADR-002: JWT-everywhere auth
  - [ ] ADR-003: separate students/parents/teachers apps
  - [ ] ADR-004: branch print agent in a separate repo
  - [ ] ADR-005: drop repository/dto/interfaces layers from `core/`
  - [ ] ADR-006: uz primary, en secondary, ru tertiary
- [ ] Onboarding doc for new backend devs
- [ ] Tenant provisioning runbook
- [ ] Eskiz / Click / Payme / Uzum integration runbooks (one each)

---

## Progress tracking

Mark items off as you ship. When an entire section is done, move it to a CHANGELOG entry and consider removing it here so the file stays scannable.

The single most load-bearing test in this whole list: **§26 first item — tenant isolation**. Write it before any feature work touches a tenant-scoped model.
