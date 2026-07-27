# ROADMAP — Starforge Edu: 5-Day Full-Build Operation

> **Mission:** take this repo from scaffold to a **100% feature-complete, tested, fast, frontend-ready** backend in 5 working days, executed by parallel AI agent sessions. TASKS.md is the product spec; this document set is the build order, the law, and the contract.
>
> **This file is the entry point. Every agent reads it first, every session, no exceptions.**

---

## 1. The document set (read in this order)

| File | What it is | When to read |
|---|---|---|
| `ROADMAP.md` (this file) | Operating model, standing technical decisions, day plan, traceability | Every session, first |
| `agents/CODE-GUIDE.md` | How to write code in this repo: layering, patterns, snippets, perf & security rules | Every session, second |
| `agents/DAY-N.md` | Your marching orders for day N: tasks, acceptance criteria, EOD gate | Your assigned day |
| `agents/TESTING.md` | How to test: fixtures, factories, the mandatory test matrix per endpoint | Before writing any test |
| `agents/API-CONTRACT.md` | API conventions + the frontend handoff (web & mobile) | Before adding/changing any endpoint |
| `agents/OWNER-ACTIONS.md` | Everything only Adrian (the owner) can provide — credentials, DNS, merchant accounts | When you hit an `[OWNER:O-x]` gate |
| `agents/WORKLOG.md` | Append-only daily log. **You MUST write an entry before ending your session** | End of every session |
| `TASKS.md` | The original exhaustive spec. Tick `[x]` as you ship | Continuously |

---

## 2. Operating model

### 2.1 Lanes

Each day runs **six parallel lanes (A–F)**. One lane = one agent session = one vertical slice of work = one feature branch. Lanes are designed to touch **disjoint apps** so parallel sessions don't conflict. Shared files (`config/urls.py`, `config/settings/base.py`, `core/permissions.py`, `pyproject.toml`) only ever receive **additive** changes — append your lines, never reorder or rewrite others' entries, and rebase on `master` before pushing.

### 2.2 Session ritual (mandatory)

**Start of session:**
1. Read `ROADMAP.md` §2–§5, `agents/CODE-GUIDE.md`, your `agents/DAY-N.md` lane section.
2. Read the **last 2 days of `agents/WORKLOG.md`** — your dependencies may have landed with caveats.
3. `git checkout master && git pull`, then branch: `dayN/lane-X-<topic>` (e.g. `day2/lane-b-attendance`).
4. Run `uv run pytest -q` on master first. If master is red, fixing it **is your first task** — log it in WORKLOG.

**End of session:**
1. All of: `uv run ruff check . && uv run ruff format --check .`, `uv run mypy apps core infrastructure config`, `uv run pytest -q` — green.
2. Tick your completed items in `TASKS.md` (`[ ]` → `[x]`).
3. **Append a WORKLOG entry** (format in `agents/WORKLOG.md`): what shipped, commits, test counts, what's blocked, handoff notes for tomorrow's lanes.
4. Conventional commits (`feat(attendance): ...`, `fix(auth): ...`, `test(payments): ...`), merge to `master` in the day's merge order (below). CI must be green on master at end of day.

### 2.3 Merge order

Within a day, merge in this order so dependents land on top of dependencies:
- **Day 1:** A (bootstrap/migrations) → C (auth/JWT) → B (tenancy) → F (org) → D (people) → E (tests)
- **Days 2–5:** A → B → C → D → E → F unless your DAY file says otherwise.
- A migration conflict (two lanes generated `000X_` with the same number) is resolved by the later merger: `python manage.py makemigrations --merge` or renumber yours.

### 2.4 Escalation

If you're blocked by something only the owner can do, **do not stall**: check `agents/OWNER-ACTIONS.md` for the gate ID, build the mock path (TD-2), mark the task `[~]` with a `BLOCKED(O-x)` note in WORKLOG, and move on. Never invent credentials, never commit secrets.

---

## 3. Definition of Done (the law)

A task is **done** only when ALL of the following hold. This list is what "don't miss anything" means — agents are graded against it:

1. **Models** — real domain models with constraints (`unique_together`/`UniqueConstraint`, `CheckConstraint`, `db_index` on every FK + every field filtered in selectors), `__str__`, `Meta.ordering`. Migration generated and committed.
2. **Per-Center configurability (TD-13)** — no magic numbers. Anything a school might want different (late thresholds, grace periods, file caps, grading scheme, currency) reads from `CenterSettings`.
3. **Services & selectors** — writes go through `services.py` functions (validated, transactional, typed signatures); reads through `selectors.py` (always `select_related`/`prefetch_related` — N+1 is a bug).
4. **Serializers** — separate read/write serializers when shapes differ; no `fields = "__all__"` on models with sensitive fields.
5. **Views** — `TenantSafeModelViewSet` (or APIView for non-CRUD), **per-action permissions** (TD-5), `object_scope` where branch/department-scoped, filtering (`django-filter`), search, ordering on list endpoints.
6. **URLs** — registered in the app's `urls.py`; included in `config/urls.py` (already done for all apps).
7. **OpenAPI** — `@extend_schema` with summary, tags, request/response examples, and error responses. The generated schema is the frontend contract — it must read well (API-CONTRACT.md §1).
8. **Signals/events** — user-facing happenings emit a signal consumed by `apps/notifications` (never call SMS/email/push adapters from a domain app); sensitive mutations land in `apps/audit` (TD-9).
9. **Async** — anything touching an external service (Eskiz, S3 processing, Anthropic, payment providers, Soliq, FCM) runs in Celery with retries + idempotency key, never inline in a request handler.
10. **Tests** — per `agents/TESTING.md`: happy path, permission-denied per role, **cross-tenant isolation**, validation edges, and a query-count assertion on list endpoints. New code ships with its tests in the same branch.
11. **i18n** — every user-facing string wrapped in `gettext_lazy`; notification/SMS templates have uz/ru/en variants.
12. **Speed** — list endpoints paginated, p95 under ~150 ms locally against seeded data (`scripts/seed_dev.py` scale); no unindexed filter.
13. **Bookkeeping** — TASKS.md ticked, WORKLOG entry appended, docs touched if behavior diverges from docs/.

---

## 4. Standing Technical Decisions (TD-x)

These are **decided**. Do not re-litigate them mid-build; if one proves wrong, write the problem in WORKLOG and flag for the owner.

- **TD-1 — Tenant binding in JWT.** Tokens carry `schema` (the issuing tenant's `schema_name`) and `tv` (user token_version) claims. A custom authentication class in `core/authentication.py` rejects any token whose `schema` ≠ `connection.schema_name` (401, code `tenant_mismatch`) and whose `tv` ≠ user's current. This kills the cross-tenant token replay hole. The tenant-isolation test (TASKS §26 item 1) is written **before** this lands, red→green.
- **TD-2 — Mock-first externals.** Every external integration (Eskiz ✓ exists, Click, Payme, Uzum, Soliq, FCM, Anthropic) has a settings-switched mock (`*_USE_MOCK`, default True outside production) implementing the same interface, deterministic enough to test against. **No feature may block on owner credentials** — build against the mock, flip the flag when `OWNER-ACTIONS` delivers.
- **TD-3 — Public-schema users.** `apps.users`, `apps.auth`, and `rest_framework_simplejwt.token_blacklist` are added to `SHARED_APPS` (kept in `TENANT_APPS` too). Public schema gets its own isolated `users_user` table for **platform staff only** — this fixes the currently-broken apex `/admin/` and `IsAdminUser` on `CenterViewSet`. Document as ADR-007.
- **TD-4 — Fail-closed permissions.** `RolePermission` denies when a view declares no permission mapping (current code silently allows — that's a bug, fix Day 1).
- **TD-5 — Per-action permissions.** Viewsets declare `required_perms: dict[action, "resource:verb"]`; helper derives defaults (`list/retrieve → :read`, `create/update/partial_update/destroy → :write`). The flat `required_perm` string is removed once all viewsets migrate. `ROLE_PERMISSION_MATRIX` gets real per-feature entries as each domain lands (read_self / read_own_children verbs get enforced by queryset scoping in selectors).
- **TD-6 — Per-tenant payment credentials.** Each Center collects tuition into **its own** Click/Payme/Uzum merchant accounts: `ProviderConfig` model (tenant schema, credentials encrypted with TD-11). Webhooks arrive on the **public schema** at `/api/v1/webhooks/<provider>/<center_slug>/`, which resolves the Center, enters `schema_context`, verifies signature against that tenant's `ProviderConfig`, and processes. Platform-subscription payments (TD-8) use the owner's own merchant credentials from env instead.
- **TD-7 — Fiscalization (Soliq).** `infrastructure/fiscal/soliq_client.py`: `FiscalClient` ABC + `MockSoliqClient` + real client for soliq.uz e-receipt submission. Called from a Celery task after a payment completes; `FiscalReceipt` model in `apps/payments` stores the fiscal sign/QR URL. Gated `[OWNER:O-5]`, mock-first.
- **TD-8 — Paywall = `apps/billing` (SHARED_APPS, public schema).** Models: `Plan` (limits: max_students, max_branches, ai_tokens_month, storage_gb, price_uzs), `Subscription` (center, plan, status: trialing/active/past_due/suspended, period dates), `UsageSnapshot`. A middleware right after `TenantMainMiddleware` returns `402 {"error":{"code":"subscription_required"}}` on tenant API routes when the subscription is suspended/expired (admin + auth routes stay reachable). Nightly Celery job meters usage and flips states. This is the paywall.
- **TD-9 — Audit is signal-driven and append-only.** `AuditLog` written via `post_save`/`post_delete` receivers registered in `apps/audit/receivers.py` for sensitive models (User, RoleMembership, Invoice, Payment, Grade, ExamResult, ProviderConfig, Subscription) + an `audit_log()` helper for non-model events (login, OTP, impersonation, exports). No update/delete API exists; the viewset is read-only with cursor pagination.
- **TD-10 — Control center = apex platform API + Django admin.** Public-schema API under `/api/v1/platform/`: center CRUD + suspend/activate, per-center usage (DAU, storage, AI tokens, student count), subscription management, read-only impersonation (short-lived scoped token, heavily audited). Django admin on the apex is the v1 UI; the API is built for the future React admin (§27).
- **TD-11 — Field encryption.** `core/fields.py` → `EncryptedTextField`/`EncryptedCharField` (Fernet, key = `settings.FIELD_ENCRYPTION_KEY`, separate from SECRET_KEY, rotation runbook in docs). Used for: `national_id`, `medical_notes`, provider credentials, Soliq tokens.
- **TD-12 — Scheduling = materialized occurrences.** `RecurrenceRule` (dateutil rrule string) generates concrete `Lesson` rows for the term window at save time; conflict detection (room/teacher/cohort overlap) runs on the materialized rows via range-overlap queries; single-occurrence edits mutate the one row (`detached_from_rule` flag).
- **TD-13 — `CenterSettings`** — singleton model in tenant schema holding all per-school knobs (grading scheme, late threshold minutes, attendance correction window hours, assignment grace, file size cap, allowed file types, currency pair + FX source, quiet hours, OTP channel prefs). One settings endpoint exposes it; everything dynamic reads from here.
- **TD-14 — Documents/PDF.** `weasyprint` for PDFs (transcripts, receipts, statements, reports), `openpyxl` for Excel. Generation always in Celery → S3 → signed URL. Templates in `templates/documents/` with uz/ru/en variants.
- **TD-15 — Realtime fan-out.** Channels groups: `user.{id}`, `cohort.{id}`, `branch.{id}` (joined at connect from RoleMemberships/profiles). `apps/notifications.services.dispatch()` is the ONLY producer that pushes to groups (via `infrastructure/websocket/channel_layer.group_send`). Consumers: `NotificationConsumer` (`/ws/notifications/`), `AttendanceConsumer` (`/ws/cohorts/<id>/attendance/`).
- **TD-16 — New deps allowed** (add to `pyproject.toml`, justify in WORKLOG): `django-filter`, `factory-boy`, `python-dateutil`, `icalendar`, `weasyprint`, `openpyxl`, `cryptography`, `firebase-admin`, `pillow`, `python-magic-bin` (win) / `python-magic`, `django-axes`, `django-csp`, `pytest-cov`. Anything else: ask via WORKLOG.
- **TD-17 — Known bugs to fix Day 1 Lane A** (found in audit, all confirmed in code): EskizClient 401-retry recurses without a guard (`infrastructure/sms/eskiz_client.py`); hardcoded sender `4546` → setting; Anthropic Redis cache key omits `max_tokens`/`effort` (`infrastructure/ai/anthropic_client.py`); `docker/entrypoint.sh` orphaned + its `migrate` skips tenant migrations; README says `claude-opus-4-7` but settings use `claude-sonnet-4-6` (align both to the settings value or bump deliberately); OTP auto-creates users on verify — add `CenterSettings.open_registration` flag, default **off** (staff must pre-create users; flip on for centers that want self-serve).
- **TD-18 — API versioning & errors.** Everything under `/api/v1/`; the error envelope is the ONE flat shape `{"success": false, "code", "message", "errors"?}` (converged in FI-1), universal across the layered views, the DRF `reports` handler, and Django's own handlers/middleware — except `/healthz/*` (ops-probe shape) and payment webhooks (provider-exact). Breaking changes post-handoff require `/api/v2/`, not mutation.
- **TD-19 — Tenant discovery for mobile.** Public endpoint `GET /api/v1/platform/resolve/?slug=demo` → `{name, base_url, ws_url, logo, locale}` so the mobile app can find its tenant API without hardcoding subdomains. Web uses the subdomain it's served from.
- **TD-20 — Tests gate merges.** Coverage floor: 70% after Day 1, 80% after Day 3, **85% after Day 5** (`pytest --cov=apps --cov=core --cov-fail-under=N` wired into CI Day 1).

---

## 5. The five days

| Day | Theme | Outcome at EOD |
|---|---|---|
| **1** | **Go live + security spine + people domain** | DB migrated & seeded, CI green incl. coverage, JWT tenant-bound, fail-closed per-action perms, platform admin works, Student/Teacher/Parent/Guardian/Cohort live, org complete |
| **2** | **Academic engine** | Schedule (recurrence+conflicts), attendance (incl. auto-absent), academics (exams→transcripts PDF), assignments (S3 submissions), content library + full signed-URL storage flow |
| **3** | **Money, fiscal & messaging** | Finance (invoices→statements), payments (Click/Payme/Uzum + webhooks + Soliq receipts, mock-first), notifications system end-to-end, append-only audit, **paywall/billing live** |
| **4** | **Intelligence, realtime & control center** | AI features (budgeted, Celery-only), reports (PDF/Excel, scheduled), Channels consumers live, printing pipeline, platform control center, i18n pass, beat schedule consolidated |
| **5** | **Hardening, speed & handoff** | Security hardening, perf audit (every endpoint fast), test matrix to ≥85%, OpenAPI polished + TS & Dart clients generated, docs/ADRs/runbooks, full E2E demo green |

### Lane map (detail lives in `agents/DAY-N.md`)

| Lane | Day 1 | Day 2 | Day 3 | Day 4 | Day 5 |
|---|---|---|---|---|---|
| **A** | Bootstrap, CI, health/req-ID/logs, TD-17 bug fixes (§0,§1) | Schedule (§9) | Finance (§15) | AI (§18) | Security hardening (§25) |
| **B** | Tenancy lifecycle + TD-3 + CenterSettings (§2) | Attendance (§10) | Payments + webhooks + Soliq (§16, TD-6/7) | Reports (§20) | Test completion → 85% (§26) |
| **C** | Auth/JWT hardening: TD-1/4/5, devices, OTP polish (§3) | Academics (§11) | Notifications (§17, TD-15 prep) | Channels realtime (§21, TD-15) | Performance audit & load smoke |
| **D** | People domain: profiles, Guardian, Cohort (§5–8) | Assignments (§12) | Audit (§19, TD-9) | Printing (§14) | API contract, OpenAPI polish, TS+Dart clients (§27) |
| **E** | Test foundation: factories, two-tenant fixture, isolation test (§26) | Content + Storage (§13,§23) | **Billing/paywall (TD-8)** | **Control center (TD-10)** + impersonation | Docs, ADRs, runbooks, deploy prep (§29,§30) |
| **F** | Org: rooms, hours, holidays, capacity (§4) | Day-2 cross-tests + perms matrix | Day-3 cross-tests + webhook attack tests | i18n (§24) + beat consolidation (§22) | Final E2E demo + release QA |

---

## 6. Traceability — every TASKS.md section has an owner

§0→D1-A · §1→D1-A · §2→D1-B (+D4-E control center) · §3→D1-C · §4→D1-F · §5→D1-D · §6→D1-D · §7→D1-D · §8→D1-D · §9→D2-A · §10→D2-B · §11→D2-C · §12→D2-D · §13→D2-E · §14→D4-D · §15→D3-A · §16→D3-B · §17→D3-C · §18→D4-A · §19→D3-D · §20→D4-B · §21→D4-C · §22→D2-B/D3-A/D4-F (consolidation D4-F) · §23→D2-E · §24→D4-F · §25→D5-A · §26→D1-E + every lane + D5-B · §27→D5-D (client generation + handoff; building the actual frontends is a separate effort on top of these artifacts) · §28→D4-D (server side; the CUPS agent is a separate repo by design — see TASKS §14 note) · §29→D5-E (compose-prod + runbooks; live hosting is `[OWNER:O-9]`) · §30→D5-E.

**Added scope beyond TASKS.md** (from the owner's brief): paywall/billing (TD-8, D3-E), control center (TD-10, D4-E), Soliq fiscalization (TD-7, D3-B), per-Center dynamic settings (TD-13, D1-B), mobile tenant discovery (TD-19, D5-D).

---

## 7. End-state acceptance ("100% finished" means this)

Run on a fresh clone, `docker compose up`, migrate, seed:

1. Provision two Centers; verify a JWT from one is rejected by the other (401 `tenant_mismatch`).
2. Full username+password login on web subdomain and via mobile-style flow (resolve → login → refresh → logout-all), plus the OTP password-reset flow (request → SMS code → confirm → fresh login). *(Owner decision 2026-06-11: login is username+password; OTP is reset/verification only.)*
3. Enroll a student (state machine), link a parent, assign to cohort, build a recurring schedule with a conflict correctly rejected.
4. Mark attendance (one absent) → guardian gets a (mock) SMS + in-app notification over a live WebSocket.
5. Create exam → enter grades → transcript PDF downloads via signed URL.
6. Assignment with S3 attachment → student submits → AI feedback task runs under budget accounting (mock or real per key).
7. Invoice issued → (mock) Payme webhook completes payment → allocation + fiscal receipt recorded → parent sees paid status; reconciliation report matches.
8. Trial expiry → paywall returns 402 on tenant API; platform admin reactivates via control center.
9. Print job queued → agent claims via token-auth endpoint → status flows to done.
10. Scheduled weekly report lands in S3, link delivered by (mock) email.
11. `pytest --cov` ≥ 85%, ruff/mypy clean, OpenAPI validates, TS + Dart clients generate without errors.
12. Every list endpoint answers < 150 ms locally on seeded data; zero N+1 found by the query-count tests.

When all 12 pass, tag `v1.0.0`, write the final WORKLOG entry, and hand `agents/API-CONTRACT.md` + generated clients to the frontend team.
