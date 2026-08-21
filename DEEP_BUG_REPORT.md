# Starforge EDU — Deep Bug Report

**Status:** 🔄 Active loop — 3 hunt rounds done, 13 fix batches committed (see "Summary of this session" at the bottom)
**Date:** 2026-07-08 · **Branch:** `day1-build` (unpushed) · **Base commit:** `dce7b83` → latest `cf311e7`

---

## What this report is

A second, independent bug hunt over the whole project, run **after** and **disjoint from** `DEEP_AUDIT_REPORT.md`.

`DEEP_AUDIT_REPORT.md` (2026-07-03) already logged **346 issues** — 100 confirmed, 246 unverified leads, 4 dismissed. Everything in that file is **out of scope here**. Every hunter in this pass is handed an exclusion index built from it (346 titles + `file:line`) and is instructed to refute-and-drop any finding that restates one.

### Why a second pass finds anything at all

The prior audit fanned out **one reader per app** (48 agents, ~1 app each). That method is strong on *intra-app* defects and structurally blind to:

| Blind spot | Why per-app readers miss it |
|---|---|
| Cross-app signal graphs | Sender in app A, receiver in app B — neither reader sees both halves |
| Tenant/cache-key collisions | The bug is the *absence* of a schema namespace, visible only across apps |
| Celery ↔ web races | The task and the view live in different files, owned by different readers |
| ORM aggregation fan-out | Needs the query *and* the model graph together |
| Migration ↔ model drift | Needs a diff of two directories, not a read of one |
| Admin / management commands | Not part of any app's request path |
| Regressions in the fix commit | `dce7b83` postdates the audit entirely |

So this pass hunts **by lens, not by app**.

## Method

- **Hunt:** cross-cutting hunter agents, each owning one lens, each required to cite `file:line` and quote the offending code.
- **Verify:** every candidate finding is attacked by **two independent adversarial refuters** with disjoint mandates:
  - *Lens A — factual correctness:* does the quoted code exist and does it really misbehave? Is there an upstream guard?
  - *Lens B — reachability & novelty:* is it reachable by a real actor? Is it a duplicate of a prior finding, or already fixed in `dce7b83`?
- A finding is **CONFIRMED** only if **both** refuters fail to break it. One refuter → **PLAUSIBLE**. Both → dropped (and listed, with the reason, so the ground isn't re-walked).
- Refuters default to `refuted=true` when they cannot positively confirm the defect from the code. This biases the report toward **fewer, harder** findings.

## Scope

Every app (38), plus `core/`, `config/`, `celery_tasks/`, `infrastructure/`, `docker/`, `scripts/`, top-level `tests/`.

## Exclusions (already covered — do not re-report)

Prior-audit finding classes, all considered closed for this report:
`PUT-behaves-as-PATCH` · `HEAD 405 divergence` · pure test-gaps · pure dead code · the four bugs fixed in `dce7b83`
(core `?ordering=--field`, `allocate_manual` per-line allocation, `due_day_of_month=0`, Payme non-dict `params`).

---

## Ground truth: the suite is no longer green on a fresh DB

The prior audit's headline was "**fresh DB, `--create-db`: 1542 passed, 0 failed**." I re-ran the same gate today and got **7 failed, 1537 passed, 6 skipped**. All 7 are in `apps/schedule/tests/test_schedule.py`:

```
test_materialize_counts_and_holiday_skip
test_materialize_idempotent
test_conflict_overlap_raises_409[teacher|cohort|room]
test_bulk_reschedule_emits_one_rescheduled_per_moved_lesson
test_deactivating_rule_clears_future_lessons_and_stops_regeneration
```

### Finding G-1 · [MEDIUM][test-quality] Schedule tests are wall-clock-date-fragile — the suite silently rots and now fails on a clean DB

- **Where:** [apps/schedule/tests/test_schedule.py:41](apps/schedule/tests/test_schedule.py#L41), `:59`, `:63`, `:116`, `:129` — and the mechanism at [apps/schedule/services/__init__.py:95](apps/schedule/services/__init__.py#L95),`:107`.
- **Root cause:** The tests pin the rule window to `start=date(2026, 7, 6)` (a Monday) with `BYDAY=MO`. `test_conflict_overlap_raises_409` uses a **single-day** window `end=date(2026, 7, 6)`. `materialize_rule` correctly skips any occurrence at/before `now`:
  ```python
  now = timezone.now()
  kept = [lf for lf in existing if ... or lf.starts_at <= now]
  for starts_at, ends_at in _rule_occurrences(rule):
      if starts_at <= now or starts_at in kept_starts:
          continue
  ```
  On the audit date (2026-07-03) `2026-07-06` was in the **future**, so lessons materialized, overlapped, and the tests passed. Today (2026-07-08) that Monday is in the **past**, so the single-day rule materializes **zero** lessons → no overlap → `DID NOT RAISE`. The count/idempotent/reschedule/deactivate tests fail for the same reason (fewer future occurrences than the hardcoded expectation).
- **Why it matters:** This is not a product defect — `materialize_rule` is behaving correctly. It is a **suite-integrity** defect: the "0 failures on fresh DB" ground truth the whole audit rests on is now false purely from calendar drift, and it will keep drifting. A genuinely broken overlap constraint would now be **indistinguishable** from this calendar rot — the safety net that should catch a real schedule regression is dead. Fix: make the window relative to `timezone.now()` (e.g. `today + 7d … + 60d`) or freeze time.
- **Verified:** `date(2026,7,6).weekday() == 0` (Monday); `date.today() == 2026-07-08`; reproduced all 3 `test_conflict_overlap_raises_409` params failing with "DID NOT RAISE".

---

## Round 1 — cross-cutting lens hunt (14 lenses)

13 raw candidates → **11 CONFIRMED, 2 PLAUSIBLE, 0 refuted** (both refuters passed all that survived). None duplicate the prior 346. Ordered by severity. **Status** tracks fix state in this session.

### R1-01 · [HIGH][money] Click/Uzum checkout sends the Payment PK as the merchant reference, but the webhook resolves the invoice by `Invoice.number` → real online payments are ACKed to the provider yet never credited ✅ FIXED

- **Where:** [apps/payments/services/__init__.py:355](apps/payments/services/__init__.py#L355),`:367` (checkout) vs [apps/payments/webhook_views.py:99](apps/payments/webhook_views.py#L99),`:190` (webhook).
- **Root cause:** `_build_provider_checkout` passed `merchant_trans_id=str(payment.pk)` (Click) / `order_id=str(payment.pk)` (Uzum). The provider echoes that reference back on the completion callback, where `click_webhook_view`/`uzum_webhook_view` do `Invoice.objects.filter(number=<ref>).first()`. Since `Invoice.number` is a formatted string (`INV-2026-…`), `filter(number="42")` returns `None` → the `if invoice is not None:` body is skipped → `mark_webhook_processed` still runs → the provider gets `ERROR_SUCCESS`. **Every real Click/Uzum payment is lost.** Payme (the sibling) correctly uses `invoice.number`; the existing tests never caught it because they call `process_click_complete(invoice=inv)` directly, bypassing checkout.
- **Fix:** checkout now emits `account["invoice"]` (= `invoice.number`), matching the webhook resolution and Payme. Regression test asserts the checkout payload's `merchant_trans_id`/`order_id` equals `invoice.number` and not the payment PK (round-trip contract).
- **Verified:** 2 refuters passed; my own read of checkout + both webhooks + `process_*` confirmed; new parametrized test green.

### R1-02 · [HIGH][money] Click/Uzum amount check compares the callback exactly against a fractional `total_uzs`, but checkout rounds to whole soum → any fractional-total invoice is permanently unpayable online ✅ FIXED

- **Where:** [apps/payments/services/__init__.py:350](apps/payments/services/__init__.py#L350) (checkout round) vs `_assert_provider_amount` at `:682`.
- **Root cause:** checkout charges `int(total_uzs.quantize(1, ROUND_HALF_UP))` (whole soum — Click/Uzum transmit soum), so a 149999.99 invoice is charged 150000. The callback reports 150000, but `_assert_provider_amount` did `if reported != invoice.total_uzs` — `Decimal("150000") != Decimal("149999.99")` → `amount_mismatch` → the payment is rejected forever. Fractional totals arise from percentage discounts.
- **Fix:** `_assert_provider_amount` now compares against `total_uzs.quantize(1, ROUND_HALF_UP)` — the same rounding checkout applied. Whole-soum totals are unaffected (quantize is a no-op) and under-payment is still rejected (the provider must report the rounded charge). Added an `is_finite()` guard (NaN/Inf → 400, not a silent pass). Payme carries exact tiyin and guards in its own client, so it never reaches here. Two regression tests: fractional invoice now payable at the rounded soum; a one-soum-short callback still rejected.
- **Verified:** 2 refuters passed; my own read confirmed; both new tests green.

### R1-03 · [MEDIUM][money] AI task budget reconciliation (`record_usage`) re-charges the tenant token budget and re-calls the paid model on any `acks_late` re-execution ⏳ TODO

- **Where:** [apps/ai/services/__init__.py:237](apps/ai/services/__init__.py#L237) (guard) with [celery_tasks/ai_tasks.py:79](celery_tasks/ai_tasks.py#L79) (caller).
- **Root cause:** the idempotency guard keys off a status transition committed in a *separate, later* save, so a Celery re-delivery (at-least-once + `acks_late`) between the model call and the status commit re-reserves budget and re-invokes the paid model. Duplicate AI spend.
- **Status:** to fix — needs a durable per-AIRequest idempotency key checked+committed atomically before the model call.

### R1-04 · [MEDIUM][bug] Stacked/uncapped invoice discounts drive the sum of persisted `InvoiceLine` rows below zero while `Invoice.total_uzs` is clamped to 0 → `sum(lines) == total_uzs` invariant broken, student silently zero-billed ⏳ TODO

- **Where:** [apps/finance/services/__init__.py:257](apps/finance/services/__init__.py#L257) (clamp), root cause `:321`–`:333` (sibling per-item cap but no aggregate cap).
- **Status:** to fix — cap aggregate discount at the invoice subtotal so persisted lines can't sum below the clamped total.

### R1-05 · [MEDIUM][security] WebSocket consumers authorize only at connect; session-revocation (force-logout / password-reset) and role-revocation never terminate a live socket ✅ FIXED (owner-approved, commit `4d46a75`)

- **Where:** [infrastructure/websocket/consumers.py](infrastructure/websocket/consumers.py), [infrastructure/websocket/middleware.py](infrastructure/websocket/middleware.py), [apps/attendance/consumers.py](apps/attendance/consumers.py).
- **Fix:** the middleware stashes the raw session-key in `scope["_ws_token"]`; `HeartbeatConsumerMixin` re-authorizes every heartbeat cycle (~30s) — re-runs `validate_session_key` in the socket's tenant schema (via `database_sync_to_async`, mirroring the middleware's schema switch). A revoked/expired session → discard groups + close 4401. An overridable `_still_authorized()` hook lets a scoped consumer re-check its gate; `AttendanceConsumer` re-runs the branch/role check → close 4403 on a revoked role. Same teardown as the heartbeat-timeout path (no group leak). +2 tests (session-revoked→4401+group discarded; attendance role-revoked→4403).

### R1-06 · [MEDIUM][bug] A Redis/channel-layer error inside the `student_marked_absent` receiver propagates out of the post-commit hook → a committed attendance mark 500s and every remaining absent student in the batch loses their guardian notification ⏳ TODO

- **Where:** [apps/notifications/receivers.py:138](apps/notifications/receivers.py#L138) → `push_cohort_attendance` → `infrastructure/websocket/channel_layer.py:11` `group_send`.
- **Status:** to fix — isolate the push in a best-effort try/except so a realtime-layer outage can't fail a committed mark or drop the rest of the batch.

### R1-07 · [MEDIUM][security] Django admin login is exempt from all rate limiting → unlimited password brute-force / credential-stuffing against staff & superuser accounts on every tenant subdomain and the apex ⏳ TODO

- **Where:** [core/middleware.py:157](core/middleware.py#L157) — `ApiRateLimitMiddleware` only guards `/api/`, so `/admin/login/` is unthrottled.
- **Status:** to fix — extend the blanket limiter (or a dedicated one) to the admin login POST.

### R1-08 · [MEDIUM][bug] Offset pagination applies no unique pk tiebreaker → paging any list ordered on a non-unique column silently drops and duplicates rows across page boundaries ⏳ TODO

- **Where:** [core/listing.py:83](core/listing.py#L83) (`apply_filters` ordering) + `paginate`.
- **Status:** to fix — append `pk` as a deterministic final sort key whenever ordering is applied; add a cross-page no-drop/no-dup test. High blast radius (every list endpoint) so it gets its own batch + tests.

### R1-09 · [LOW][race] `transition_task` mutates an unlocked, pre-fetched `Task` row (check-then-act, no `select_for_update`) → concurrent transitions can bypass the state-machine graph (e.g. land a CANCELLED task in DONE) ⏳ TODO

- **Where:** [apps/tasks/services/__init__.py:127](apps/tasks/services/__init__.py#L127) — the only status-transition service that omits the row lock every sibling holds (approvals/covers/meetings/sales/loans/finance/achievements/printing all re-fetch under `select_for_update`).
- **Note:** refuter corrected the reported "CANCELLED with completed_at set" — `completed_at` stays coherent (same `update_fields`). The real harm is a forbidden transition (`CANCELLED→DONE`) via two racers reading the same `OPEN` pre-image. LOW (internal to-do state, recoverable, both actors already authorized).
- **Status:** to fix — one line: re-fetch `Task.objects.select_for_update().get(pk=task.pk)` inside the atomic.

### R1-10 · [LOW][bug] `dispatch_notification`'s per-channel idempotency guard is check-then-act with no row lock and no unique constraint on `NotificationDelivery(notification, channel)` → a concurrent Celery redelivery double-sends SMS/email/push for one event ⏳ TODO

- **Where:** [celery_tasks/notification_tasks.py:64](celery_tasks/notification_tasks.py#L64).
- **Status:** to fix — add a unique constraint on `(notification, channel)` (DB-backed idempotency) + `get_or_create`.

### R1-P1 · [MEDIUM][bug] (PLAUSIBLE — 1 refuter dissent) Nightly billing metering fans out per-tenant synchronously in a bare loop with no isolation → one center's exception aborts usage snapshots **and** subscription state-flips (`past_due`/`suspended`) for every center after it

- **Where:** [celery_tasks/billing_tasks.py:41](celery_tasks/billing_tasks.py#L41). Needs re-verification (the prior audit reported a *similar* class for other tasks; confirm this is the billing instance and not a duplicate) before fixing.

### R1-P2 · [LOW][money] (PLAUSIBLE — 1 refuter dissent) Invoice admin leaves `total_uzs` writable and its editable `InvoiceLine` inline never recomputes it → admin line edits silently desync the stored total from its lines

- **Where:** [apps/finance/admin.py:23](apps/finance/admin.py#L23). Low urgency (admin-only, trusted actor); fix by making `total_uzs` readonly + recompute on inline save.

### Also fixed this batch (batch-1 `dce7b83` regressions found while fixing the above)

- **B1-mypy-1:** `apps/finance/services/__init__.py` used `list[dict[str, Any]]` in `allocate_payment_lines` (added by `dce7b83`) without importing `Any` — mypy error, masked at runtime only by `from __future__ import annotations`. **Fixed** (added the import). Contradicts the memory's "mypy clean baseline" — batch-1 shipped two mypy regressions.
- **B1-mypy-2:** `core/listing.py:85` passed `str | None` to `order_by` (batch-1's `--field` fix) — mypy `arg-type` error. **Fixed** behavior-equivalently by guarding on `if ordering:` (narrows to `str`).

---

## Round 2 — deeper cross-cutting hunt (16 lenses: money-flow tracing, cross-app integrity, scale)

19 raw → **17 CONFIRMED, 2 PLAUSIBLE, 0 refuted**. CONF #1 and CONF #7 are the same bug found by two lenses (merged as **R2-01**). PLAUS #1/#2 substantially match prior-audit #109/#110 (known, still unfixed — fixing anyway). **Status** tracks fix state.

### R2-01 · [HIGH][security/money] Reward-kind self-dealing: a cash reward's recipient can approve **and** disburse their own payout — the beneficiary maker-checker guard is gated to `KIND_LOAN` only ⏳ TODO

- **Where:** [apps/approvals/services/__init__.py:715](apps/approvals/services/__init__.py#L715) (`_assert_not_loan_self_dealing`, called at approve `:735` + disburse `:809`); reward request built at [apps/rewards/services/__init__.py:72](apps/rewards/services/__init__.py#L72).
- **Root cause:** `_assert_not_self_approval` blocks only the *requester*. `_assert_not_loan_self_dealing` — the *beneficiary* guard — is hard-coded `if req.kind == KIND_LOAN and req.payload["borrower_id"] == actor.id`. A cash reward is economically identical (money-OUT to a named staff user via `payload["recipient_id"]`/`party_label`) but `kind="reward"`, so the guard is a no-op. `scoped_requests` exposes every request to any `approvals:approve`/`approvals:disburse` holder, so the recipient reaches their own reward request. Manager grants → recipient (if they hold approve/disburse) self-approves and self-pays. The exact self-dealing the loan guard exists to prevent, rewards silently exempt.
- **Fix plan:** generalize the beneficiary guard — block the actor from approving/disbursing any request whose beneficiary (`borrower_id`/`recipient_id`/whatever the kind pins as payee) is the actor, for every money-OUT-to-named-party kind (loan, reward, and any future one).

### R2-02 · [HIGH][transaction-boundary] `WebhookEvent` is committed as `RECEIVED` before side effects run; a non-`ValidationException` failure during processing makes the provider's retry get dedup-swallowed as `DUPLICATE` → permanent payment loss ⏳ TODO

- **Where:** [apps/payments/webhook_views.py:96](apps/payments/webhook_views.py#L96) (click) / `:187` (uzum); `record_webhook_event` at [apps/payments/services/__init__.py:394](apps/payments/services/__init__.py#L394).
- **Root cause:** `record_webhook_event` is its own atomic and commits `WebhookEvent(status=RECEIVED)` before `process_*_complete` runs (a separate atomic; no `ATOMIC_REQUESTS`). The view catches only `except ValidationException → mark_webhook_rejected`. Any other exception (a Postgres deadlock/serialization error on the `select_for_update` in `mark_payment_completed`, or a non-`ValidationException` from `allocate_payment`) propagates; the `RECEIVED` event stays committed. On the provider's retry, `record_webhook_event` sees `existing.status == RECEIVED` → flips to `DUPLICATE` → view returns "Already processed". The provider marks the order paid and stops retrying, but no Payment was ever created. **Customer paid, invoice unpaid forever.**
- **Fix plan:** broaden the webhook views' `except` to any exception → `mark_webhook_rejected` (so a retry reprocesses), and/or only mark the event terminal after the side effect commits.

### R2-03 · [HIGH][data-loss] Repeat lesson-reschedule notifications silently suppressed by a lesson-id-only dedupe key — students/parents never told the lesson moved *again* ⏳ TODO

- **Where:** [apps/notifications/receivers.py:283](apps/notifications/receivers.py#L283) (`on_lesson_rescheduled`), dedupe at `services/__init__.py:123`.
- **Root cause:** dispatches `dedupe_prefix=f"schedule.lesson_rescheduled:{lesson_id}"` → key has only `lesson_id + uid`, no time/version discriminator. `dispatch()` does `get_or_create(dedupe_key=...)` and on a hit returns the existing row without re-queuing. This is the exact anti-pattern the sibling receivers avoid — `on_grade_changed` appends `new_score`, `on_submission_graded` appends `score`. A lesson moved twice: the second move hits the stale key and is dropped. The highest-impact reschedule (the latest) goes silent.
- **Fix plan:** append the new `starts_at` (ISO) to the dedupe key so each distinct move notifies.

### R2-04 · [MEDIUM][concurrency/money] `enroll_student_in_cohort` never enforces the F2-6 single-active-cohort invariant → a student accumulates multiple simultaneous active memberships **and** double finance auto-issue 🚩 FLAGGED — OWNER DECISION (not fixed)

**Reassessed during fixing — this is a product ambiguity, not a clear bug.** The codebase contradicts itself on whether a student may hold multiple *simultaneous* active cohort memberships:
- **Multi-cohort is assumed valid** by `test_student_sees_lessons_from_both_active_cohorts` and `test_parent_sees_childs_active_cohort_lessons` ([apps/schedule/tests/test_schedule.py:360](apps/schedule/tests/test_schedule.py#L360)) — both enroll one student into two cohorts and assert lessons from **both** are visible. Attendance/content scoping joins on active `CohortMembership` (plural).
- **Single-cohort is the invariant** per `move_student` (end-dates ALL active memberships), the single `StudentProfile.current_cohort` FK, and `test_move_student_leaves_exactly_one_active_membership`.

Making `enroll` end-date prior memberships (the finding's fix) would **break the two schedule tests** and disable an apparently-intended feature (a student taking English + Math). Leaving it means `current_cohort` is ambiguous and each cohort bills separately (plausibly correct — two courses, two fees). **I will not silently pick a side.** Owner decision needed: *is simultaneous multi-cohort enrollment a supported feature?* If **yes** → make `current_cohort` a derived/"primary" concept and confirm per-cohort billing is intended. If **no** → make `enroll` mirror `move_student` (lock + end-date others) and update the two schedule tests. No code changed.

- **Where:** [apps/cohorts/services/__init__.py:28](apps/cohorts/services/__init__.py#L28) (enroll guard at `:36`); contrast `move_student` lock at `:72` + end-date-all at `:82`.
- **Root cause:** enroll's only duplicate guard is per-cohort (`filter(cohort=cohort, student=student, end_date__isnull=True)`). It never checks/ends active memberships in OTHER cohorts and never takes the F2-6 student lock. The DB partial-unique is also per-cohort. Enrolling S (active in A) into B leaves two active memberships, overwrites `current_cohort`, and `finance.auto_issue_on_enrollment` bills BOTH cohorts (dedupe is per fee_schedule+period).
- **Fix plan:** decide semantics with owner-sensible default — enroll should end-date prior active memberships under the same student lock `move_student` uses (single active cohort is the modeled invariant), OR 409 if already active elsewhere. Default: mirror `move_student` (lock + end-date others).

### R2-05 · [MEDIUM][money] A post-approval attendance correction (absent→present) leaves the materialized single-use absence-deduction `Discount` active → student silently credited for a lesson they attended ⏳ TODO

- **Where:** [apps/attendance/services/__init__.py:132](apps/attendance/services/__init__.py#L132) (`mark_attendance`) vs `_apply_absence_deduction_effect` at `apps/approvals/services/__init__.py:535`.
- **Root cause:** the deduction materializes a standing `Discount(single_use=True)` at approve time; the only back-link is `payload__attendance_id` (used only for the create/approve dup guard). Re-marking the record present mutates `status` but nothing reads it afterward — no receiver deactivates the discount. Directors can re-mark past the correction window. The credit auto-applies to the next invoice with no audit trail. (Distinct from prior #21 which is about a *deleted* record at approve time.)
- **Fix plan:** on absent→present correction, deactivate any active single-use discount linked to that attendance record (add the back-link + a correction hook).

### R2-06 · [MEDIUM][scale] Report generators materialize an entire unbounded table into memory (no row cap) before rendering PDF/XLSX → OOM-kills the shared Celery worker ⏳ TODO

- **Where:** [apps/reports/generators/attendance.py:46](apps/reports/generators/attendance.py#L46) (+ grades.py, enrollment.py); driven by `celery_tasks/report_tasks.py`.
- **Root cause:** `collect()` does `for rec in qs:` with OPTIONAL date filters; a full-scope director with no dates loads EVERY attendance record (millions over a center's life) into a list of dicts, then renders a giant HTML/openpyxl in memory. Contrast `apps/audit` export which caps at `MAX_EXPORT_ROWS` + streams via `iterator()`. Worker OOMs; being tenant-shared, co-running tenant tasks die too; 3 retries each re-OOM.
- **Fix plan:** cap rows (refuse/paginate over a limit) + stream with `iterator()`, mirroring the audit export. NOTE: `apps/reports` is the one DRF app — fix stays inside it.

### R2-07 · [MEDIUM][idor] Achievement grant resolves the recipient student unscoped → a branch-scoped teacher can grant a GLOBAL achievement to another branch's student (cross-branch write + student-pk oracle) ⏳ TODO

- **Where:** [apps/achievements/services/v1/achievement_service.py:104](apps/achievements/services/v1/achievement_service.py#L104) (`_resolve_student`), via `achievement_grant_view`.
- **Root cause:** the achievement is fetched branch-scoped, but the recipient comes straight from the body: `StudentProfile.objects.filter(pk=student_id).first()` with no branch check. A GLOBAL (branch=None) achievement has no cohort/branch guard, and `achievements:write` is a branch-scoped role (TEACHER/HOD). Every sibling student-write path (sales, cards, compliance) enforces `student.branch_id in branch_ids`; this one omits it.
- **Fix plan:** scope `_resolve_student` to the actor's branches (404 out-of-branch), mirroring the sales/cards/compliance pattern.

### R2-08 · [MEDIUM][security] `RecurrenceRule` create/update resolves term/cohort/teacher/room/lesson_type by pk only → cross-branch schedule injection + existence oracle ⏳ TODO

- **Where:** [apps/schedule/services/v1/schedule_service.py:148](apps/schedule/services/v1/schedule_service.py#L148) (`_resolve_fks`); view only checks `schedule:write`.
- **Root cause:** `filter(pk=value).first()` for each FK with no branch-consistency check; `TimeSlot` in the same file DOES `assert_branch_id_in_scope`, proving intent. A branch-A writer can create a rule under branch-B's cohort/teacher/room, materializing lessons (and downstream attendance/absence-deduction rows) in a branch they don't control.
- **Fix plan:** assert the resolved cohort/teacher/room belong to the actor's branch scope (and are mutually consistent), 400/403 otherwise.

### R2-09 · [MEDIUM][data-loss] Click/Uzum Complete callback for an **unresolved** invoice is ACKed as SUCCESS and marked PROCESSED with no Payment → captured money silently lost, retry blocked ⏳ TODO

- **Where:** [apps/payments/webhook_views.py:96](apps/payments/webhook_views.py#L96) (click) / `:187` (uzum).
- **Root cause:** `if invoice is not None:` — when the lookup misses (invoice deleted/renumbered after checkout), the block is skipped and control falls to `mark_webhook_processed` + success. No Payment, and the PROCESSED event blocks any corrective retry. (R1-01 fixed the *common* cause of the miss; this is the residual genuine-miss handling.)
- **Fix plan:** when a Complete callback can't resolve its invoice, `mark_webhook_rejected` (retryable) rather than mark processed + success.

### R2-10 · [MEDIUM][n+1] `grants_of()` eager-loads the wrong FKs → a per-row query for the achievement on `GET /achievements/<pk>/grants/` ⏳ TODO

- **Where:** [apps/achievements/repositories/achievement_grant_repository.py:33](apps/achievements/repositories/achievement_grant_repository.py#L33).
- **Root cause:** returns `achievement.grants.select_related("student","granted_by")` but the presenter reads those only as ids and dereferences `g.achievement` (NOT select_related) as a full object → one SELECT per grant. Sibling `grants_for_students()` correctly `select_related("achievement",...)`. For a school-wide achievement granted to thousands, page renders 1 + page_size queries.
- **Fix plan:** `select_related("achievement")` (drop student/granted_by — read as ids), or reuse the loaded `achievement` instance.

### R2-11 · [MEDIUM][scale] Cohort notification fan-out runs synchronously in the request thread — `bulk_reschedule` issues O(lessons × members) inline `dispatch()` queries per HTTP request ⏳ TODO

- **Where:** [apps/notifications/receivers.py:102](apps/notifications/receivers.py#L102) (`_dispatch_many`), driven by `bulk_reschedule` on_commit loop.
- **Root cause:** one `on_commit` emit per shifted lesson, each looping every active cohort member calling `dispatch()` inline (~3-4 queries/recipient). Unlike `announce_cohort` which offloads to chunked Celery. N=50 lessons × M=30 members ≈ 1500 dispatches / 4500+ queries in one request → connection saturation + timeout at scale.
- **Fix plan:** offload cohort fan-out to a chunked Celery task like `announce_cohort`.

### R2-12 · [LOW][security] Loan-request creation resolves the branch FK by pk with no branch-scope check → cross-branch money-request attribution ⏳ TODO
- **Where:** [apps/loans/views/v1/loan_views.py:151](apps/loans/views/v1/loan_views.py#L151). Every READ path here is branch-scoped; the write path accepts an arbitrary branch. Mis-attributes the OUT disbursement + repayments to another branch's books; branch-id oracle. **Fix:** `assert_branch_id_in_scope` on the supplied branch.

### R2-13 · [LOW][boundary] membership-as-of-date uses `lesson.starts_at.date()` (UTC) instead of center-local → off-by-one for lessons in the 00:00–04:59 Asia/Tashkent window 🚩 DEFERRED — needs a coordinated sweep (not a single-site fix)
- **Where:** [apps/attendance/services/__init__.py:113](apps/attendance/services/__init__.py#L113) + `:168`. `USE_TZ=True`, `TIME_ZONE=Asia/Tashkent`.
- **Reassessed during fixing:** the finding is real, but the fix is NOT a single call site. `move_student` (apps/cohorts/services) sets membership `start_date`/`end_date` via `timezone.now().date()` (UTC) too, and other services use the same idiom. The attendance check and the membership dates are currently *consistent* (both UTC), so they agree except at the boundary. Changing only attendance to `localtime` introduces an inconsistency that fails `test_attendance_tolerates_a_student_moved_after_the_lesson` at the boundary. **The correct fix is a coordinated codebase-wide `timezone.now().date()` → `timezone.localdate()` sweep** (every place that means "the center's calendar day"), verified per site — a dedicated task, deferred rather than half-done here. Reverted the single-site change to keep the codebase internally consistent.

### R2-14 · [LOW][migration-drift] `RoleMembership` uniqueness silently unenforced for branch-level grants (nullable `department` in `unique_together` → NULL != NULL) ⏳ TODO
- **Where:** [apps/users/models.py:208](apps/users/models.py#L208). Branch-level grants (the common case, `department=None`) fall outside the unique constraint → duplicate role rows possible; a single-row revoke leaves the role live via the survivor. Admin/ORM-only reachable today (no HTTP create path). **Fix:** partial unique constraints (one for `department IS NULL`, one for NOT NULL) or a functional unique.

### R2-15 · [LOW][scale] `_consecutive_push_failures` scans an unbounded, growing set of PUSH deliveries on every push failure (no usable index) ⏳ TODO
- **Where:** [celery_tasks/notification_tasks.py:311](celery_tasks/notification_tasks.py#L311). Filters `channel + notification__user_id + provider_response__device_id` (JSON key, unindexable) sorting all matching PUSH rows; the `(notification, channel)` index is unusable (no notification pk in the filter). A push storm to stale tokens degrades superlinearly as history grows. **Fix:** add a targeted index (e.g. on `(channel, created_at)` + a stored `device_id` column) or bound the scan window by time.

### R2-16 · [LOW][money-consistency] Click/Uzum record & fiscalize the exact fractional invoice total, but the customer is charged the HALF_UP whole-soum amount — a reconciliation-invisible charged-vs-recorded tiyin divergence ⏳ TODO
- **Where:** [apps/payments/services/__init__.py:712](apps/payments/services/__init__.py#L712) (record) vs `:350` (charge). Sub-soum per txn (defunct tiyin) so LOW, and my R1-02 fix does NOT create a stuck invoice (the fractional total is recorded and marks PAID). But charged (whole soum) ≠ recorded/fiscalized (fractional). **Fix (deferred, needs finance decision):** either record the amount actually charged, or quantize invoice totals to whole soum at issue.

### R2-P1 · [HIGH][concurrency] (PLAUSIBLE; ≈ prior-audit #109) `void_invoice` is a check-then-act race with `allocate_payment` — a concurrent payment lands on an invoice that then gets marked VOID, orphaning a live allocation on a voided bill ⏳ TODO
- **Where:** [apps/finance/services/__init__.py:360](apps/finance/services/__init__.py#L360). `void_invoice` doesn't `select_for_update` the invoice (siblings `extend/restore_invoice_due_date` do). **Fix:** lock + re-check status/allocations inside the transaction. Known (prior #109), still unfixed — will fix.

### R2-P2 · [MEDIUM][state-machine] (PLAUSIBLE; ≈ prior-audit #110) A partially-paid invoice can never reach OVERDUE — `_refresh_invoice_status` downgrades OVERDUE→PARTIALLY_PAID and the beat's overdue-flip only targets `status=ISSUED` ⏳ TODO
- **Where:** [apps/finance/services/__init__.py:474](apps/finance/services/__init__.py#L474) + `:942`. A `?status=overdue` filter silently omits delinquent partially-paid bills. **Fix:** extend the overdue-flip to past-due `PARTIALLY_PAID` too. Known (prior #110), still unfixed — will fix.

---

## Round 3 — deep per-app hunt + adversarial review of this session's fixes (12 areas)

13 raw → **8 CONFIRMED, 4 PLAUSIBLE, 1 refuted**. The fix-regression reviewer found **no regression** in any of the 13 committed fixes. New findings:

### R3-01 · [HIGH][security] Print-job `payload_s3_key` was unvalidated → any `printing:write` staffer could mint a presigned download URL for ANY object in the shared bucket (cross-tenant + cross-permission file exfiltration) ✅ FIXED (batch J, `eb6fce5`)
- **Where:** [apps/printing/views/v1/printing_views.py:206](apps/printing/views/v1/printing_views.py#L206) → agent-claim `presign_download`. The HTTP create path now requires the caller's `{current_schema()}/` prefix (mirrors the assignments attachment-key guard); internal hand-offs use the service layer and are unaffected. +test.

### R3-02 · [MEDIUM][subscription] Reactivating a lapsed center never extended `current_period_end` → the nightly meter re-suspended it within a day (activate a no-op in prod) ✅ FIXED (batch K, `6cd7459`)
- **Where:** [apps/billing/services/__init__.py:381](apps/billing/services/__init__.py#L381) `change_subscription`. Now grants a fresh cycle (`PLATFORM_EXTENSION_DAYS`) when reactivating a lapsed sub; a future period is untouched. +2 tests.

### R3-03 · [MEDIUM][subscription] `extend_trial` pushed `Center.trial_ends_at` but never synced `Subscription.current_period_end` → billing meter suspended (402) at the ORIGINAL trial end ✅ FIXED (batch K, `6cd7459`)
- **Where:** [apps/tenancy/services/__init__.py:209](apps/tenancy/services/__init__.py#L209). New `billing.extend_trial_period` re-establishes the invariant. +test.

### R3-04 · [MEDIUM][scale] JSON grade-results endpoint accepted an uncapped array with per-row DB queries (CSV twin caps at 5000) ✅ FIXED (batch L, `cf311e7`)
- **Where:** [apps/academics/views/v1/academics_views.py:345](apps/academics/views/v1/academics_views.py#L345). Now capped at `MAX_IMPORT_ROWS`. +test.

### R3-05 · [LOW][perf/500] `submit_attempt` / writing-marking over-locked the shared `PlacementTest` row via `select_for_update().select_related(...)` → serialized concurrent submits (+ 500 on `lock_timeout`) ✅ FIXED (batch L, `cf311e7`)
- **Where:** [apps/placement/services/__init__.py:365](apps/placement/services/__init__.py#L365),`:731`,`:781`. Now `select_for_update(of=("self",))` — locks only the attempt row.

### R3-06 · [LOW][correctness] Cover-assign accepted an offboarded (login-disabled) teacher → reassigned a live lesson to someone who can't act on it ✅ FIXED (batch L, `cf311e7`)
- **Where:** [apps/covers/services/v1/cover_service.py:91](apps/covers/services/v1/cover_service.py#L91). `_resolve_teacher` now rejects an inactive user. +test.

### R3-07 · [LOW][idor] Department head resolved by pk with no branch-consistency check 🚩 DEFERRED
- **Where:** [apps/org/services/v1/department_service.py:56](apps/org/services/v1/department_service.py#L56). A branch-scoped IT (org:write) could set a dept's head to a foreign-branch teacher. **Deferred:** the "head's branch" is ambiguous (a head may be a HOD resolved as a `User` vs a teacher with a profile branch); a wrong branch-consistency check could break legit assignments. LOW + limited reach (only branch-scoped IT; DIRECTOR is legitimately unscoped, and IT already has tenant-wide `users:read` so the oracle adds little).

### R3-08 · [LOW][concurrency] `enqueue_print` idempotency is a non-locking SELECT-then-CREATE (no partial unique) → concurrent identical hand-offs double-print 🚩 DEFERRED (needs a migration)
- **Where:** [apps/printing/services/__init__.py:175](apps/printing/services/__init__.py#L175). Fix = a partial unique constraint on open jobs (migration) — batched with the other migration-LOWs.

### R3-09 · [LOW][privacy] Branch-ranking k-anonymity checks the active-student headcount, not the distinct contributing-student count per metric 🚩 DEFERRED
- **Where:** [apps/intelligence/selectors.py:260](apps/intelligence/selectors.py#L260). A branch with ≥3 active students but only one graded student exposes that individual's exact grade behind an "aggregate". Fix needs a per-metric distinct-contributor count; subtle, deferred (LOW, scoped to staff with existing student visibility).

### R3-P1 · [LOW][security] StudentProfile enumeration oracle in transcript request (400 for nonexistent vs 403 for existing-but-unauthorized) 🚩 DEFERRED (LOW; PLAUSIBLE)
- **Where:** [apps/academics/views/v1/academics_views.py:507](apps/academics/views/v1/academics_views.py#L507). Reorder auth-before-existence (or uniform 404). LOW.

### R3-P2 · [LOW][concurrency] Root-level folder name uniqueness not DB-enforced (nullable parent → NULL-distinct) 🚩 DEFERRED (needs a migration; PLAUSIBLE)
- **Where:** [apps/content/models.py:129](apps/content/models.py#L129). Fix = a partial unique on `(library, name) WHERE parent IS NULL` — batched with the migration-LOWs.

### R3-P3 · [MEDIUM][correctness] Exam-generation idempotency key omits all exam params (keyed on subject only) → a differently-parameterized second request silently returns the stale first result ✅ FIXED (owner-approved, commit `b685933`)
- **Where:** [apps/ai/services/__init__.py](apps/ai/services/__init__.py) `make_idempotency_key` / `check_and_reserve_budget` / `request_exam_generation`. Implemented exactly the surgical, **opt-in** extension the flag recommended: both helpers take an optional `params` dict and, when given, append a stable sha256 suffix to the key; `request_exam_generation` passes `{exam_type, question_count, difficulty}`. Passing no params leaves the key unchanged, so the **other 6 AI features are unaffected** (no engine-wide contract change) — only exam generation opts in. Same subject + different exam shape now generates a NEW exam; identical params stay idempotent. +1 test.

---

## Round 4 — celery/config/core/money-resweep hunt (9 lenses)

8 raw → **7 CONFIRMED, 1 PLAUSIBLE, 0 refuted**. New findings:

### R4-01 · [MEDIUM][money-loss] Cash-payment endpoint passed no idempotency key → a second legitimate cash payment on the same invoice in one shift was silently swallowed ✅ FIXED (batch M, `25140d4`)
- **Where:** [apps/payments/views/v1/payment_views.py:226](apps/payments/views/v1/payment_views.py#L226) → `create_cash_payment` derived key `cash:{schema}:{invoice}:{shift}`. Now honours an `Idempotency-Key` header, else a unique key per call. +test.

### R4-02 · [MEDIUM][money-path] Blanket anon rate limiter throttled payment webhooks (all a provider's callbacks share one IP-keyed 60/min bucket) → 429 broke the always-200 contract ✅ FIXED (batch M, `25140d4`)
- **Where:** [core/middleware.py:177](core/middleware.py#L177). Webhook paths (`/api/v1/webhooks/`) now exempt (signature-authed + provider-retried). +test.

### R4-03 · [MEDIUM][reliability] `build_report` acks_late but `build_report_run` early-returned on non-QUEUED → a hard worker crash mid-render stranded the run in RUNNING forever ✅ FIXED (batch N, `36840f4`)
- **Where:** [apps/reports/services.py:104](apps/reports/services.py#L104). A RUNNING run is now re-driven on redelivery (idempotent render); only DONE/FAILED short-circuit.

### R4-04 · [MEDIUM][cost/DoS] Statement-PDF endpoint had no rate limit or dedupe (unbounded WeasyPrint+S3 per POST) ✅ FIXED (batch N, `36840f4`)
- **Where:** [apps/finance/views/v1/finance_views.py:688](apps/finance/views/v1/finance_views.py#L688). Added a per-(schema,user) 10/min `check_rate`, mirroring the sibling expensive enqueues.

### R4-05 · [MEDIUM][correctness] Nightly `meter_center`/`aggregate_center` stamped `UsageSnapshot` with the UTC date while the read side uses `localdate()` → off-by-one dashboard/DAU in the 00:00–05:00 Tashkent window ✅ FIXED (batch N, `36840f4`)
- **Where:** [celery_tasks/billing_tasks.py:68](celery_tasks/billing_tasks.py#L68), [celery_tasks/report_tasks.py:103](celery_tasks/report_tasks.py#L103). Both use `timezone.localdate()` now (consistent with readers + the AI-overage charge). NOTE: isolated fix (read side already localdate) — distinct from the deferred R2-13 which needs a coordinated sweep.

### R4-06 · [LOW][secret-exposure] 7-day signed iCal token is a URL-path credential, logged plaintext by gunicorn's access log 🚩 DEFERRED (deploy-config mitigation)
- **Where:** [apps/schedule/urls.py:26](apps/schedule/urls.py#L26) + `docker/entrypoint.sh`. Fix = redact the access-log path / shorter-lived opaque token — a deploy-config change, deferred.

### R4-07 · [MEDIUM][scale/race] Two daily beat tasks both meter every tenant and upsert the same `UsageSnapshot(center,date)` → duplicated full cross-tenant scan (2N) 🚩 FLAGGED — ownership decision
- **Where:** `config/settings/base.py` `run-nightly-metering` + `nightly-platform-aggregation`. Post-R4-05 the two writes agree (same value, same local date) so it's not corruption, but it's 2× the nightly metering load. Deduping requires deciding which task owns the snapshot (meter_center also does the AI-overage charge). Flagged, not silently changed.

### R4-P1 · [MEDIUM][idor] Print-job key guard enforces the tenant prefix but not object ownership → a branch `printing:write` holder + agent token can presign-download any in-tenant transcript/receipt/statement/report 🚩 FLAGGED — needs redesign (`task_16ac6823`)
- **Where:** [apps/printing/views/v1/printing_views.py:206](apps/printing/views/v1/printing_views.py#L206). The R3-01 fix closed the cross-tenant HIGH; this intra-tenant cross-permission residual needs the HTTP create path to **derive** the S3 key from an authorized `source_id` rather than trust a client-supplied key (no dedicated printing prefix exists to restrict to). Flagged for the owner.

---

## Owner-handed tasks — implemented (batch R, this session)

The owner handed back the two flagged task prompts (R2-04, R4/PLAUS1) as a go-ahead.

- **R2-04 — implemented as "multi-cohort IS a feature".** The evidence is decisive (explicit schedule feature-tests + plural-membership scoping in attendance/content); `move_student`'s single-active behavior is correct *for a move*. `enroll_student_in_cohort` no longer silently reassigns `current_cohort` on a **secondary** enroll — it's now the student's **stable PRIMARY** (set on first enroll; a MOVE is the explicit way to change it). Per-cohort billing (one auto-issued invoice per cohort's fee schedule) is the intended behavior — **kept, documented, no billing change**. +test. **NOTE:** the owner also started this chip in a separate session, so there may be a parallel implementation to reconcile at merge; this one is committed on `day1-build`.
- **R4/PLAUS1 — safe high-value core done; object-level redesign still tracked (`task_16ac6823`).** `_create_job` now requires the **owning resource's read permission** per source (transcript→`academics:read`, receipt→`finance:read`, report→`reports:read`, assignment→`assignments:read`), closing the cross-permission-**class** exfil (a `printing:write` registrar/cashier/librarian/security can no longer pull finance/academic documents they can't read). The **complete** fix — deriving the S3 key from `source_id` (not the client key) + object-level scope — remains the follow-up: the four sources have **non-uniform keys** (assignment is uuid-based, not PK-derivable) and object-level authz (students/parents hold `academics:read`, teachers are cohort-scoped) that must reuse each owning app's scoping. Reimplementing that hastily risks new holes, so it's left as a scoped redesign.

---

## Round 5 — money-chain E2E + adversarial re-review of this session's 16 batches + IDOR re-check (6 lenses)

8 raw → **4 CONFIRMED, 3 PLAUSIBLE, 1 refuted**. The re-review caught **three incomplete edges in this session's own fixes** — exactly what it was for.

### R5-01 · [HIGH][money/ledger] Refund names one invoice but the reversal released the payment's allocations across ALL invoices → a refund of A silently reopened B ✅ FIXED (batch Q, `8c57865`)
- **Where:** [apps/finance/services/__init__.py:777](apps/finance/services/__init__.py#L777). `reverse_allocations_for_payment` now takes `invoice_id`; `register_refund_completion` scopes the reversal to the Refund's named invoice. +test.

### R5-02 · [HIGH][security] rule bulk-reschedule action omitted the branch-scope guard R2-08 added to create/update/detail (gap in my own fix) ✅ FIXED (batch P, `8b8ebad`)
- **Where:** [apps/schedule/views/v1/schedule_views.py:511](apps/schedule/views/v1/schedule_views.py#L511). Added `assert_branch_id_in_scope(request, rule.cohort.branch_id)`.

### R5-03 · [MEDIUM][correctness] Auto AI-feedback fabricated a visible `score=0` grade on every ungraded submission ✅ FIXED (batch Q, `8c57865`)
- **Where:** [apps/assignments/presenters.py:35](apps/assignments/presenters.py#L35). A not-human-graded placeholder (`graded_by=None`) now presents `score=null` + `graded=false`; a teacher's real 0 stands. +test.

### R5-04 · [MEDIUM][serialization] Datetime renders `+00:00` (presenters via `.isoformat()`) vs `Z` (compute-on-read selectors via DjangoJSONEncoder) for the SAME field 🚩 FLAGGED — broad contract decision
- **Where:** e.g. [apps/finance/presenters.py:30](apps/finance/presenters.py#L30) vs [apps/finance/selectors.py:120](apps/finance/selectors.py#L120). The pre-migration DRF contract was `Z`; the presenter-based bulk of the API diverged to `+00:00`. **Flagged, not fixed:** normalizing is a sweep across every presenter timestamp + every timestamp test assertion, and a decision on the canonical format (the frontend is a separate track) — a coordinated change like R2-13, not a safe autonomous edit.

### R5-P1 · [MEDIUM] (gap in R2-05, PLAUSIBLE) A PENDING absence-deduction corrected before approval still credited the student — approve did no attendance re-check ✅ FIXED (batch P, `8b8ebad`)
- `_apply_absence_deduction_effect` now re-asserts the locked record is still ABSENT/EXCUSED at approve time.

### R5-P2 · [MEDIUM] (gap in R2-03, PLAUSIBLE) Reschedule dedupe keyed on `old_start` alone re-collided on move-back (A→B→A→B) ✅ FIXED (batch P, `8b8ebad`)
- Emit `moved_at` (the lesson's monotonic post-save `updated_at`) and key on it. +test (3 moves, repeated old_start, 3 distinct keys).

### R5-P3 · [MEDIUM] (revises R4/CONF6, PLAUSIBLE) The cash fallback key was a fresh uuid = zero double-submit protection ✅ FIXED (batch P, `8b8ebad`)
- Fall back to an amount-derived key: a same-amount resubmit coalesces, distinct-amount partials still split. +test.

---

## Round 6 — re-review of latest fixes + final money/webhook/authz/tenancy/endpoint sweep (6 lenses)

8 raw → **4 CONFIRMED, 3 PLAUSIBLE, 1 refuted**. This round re-reviewed batches 15–20 (not covered by round 5's re-review) and **caught a HIGH money regression I introduced in batch Q**, plus the cash-idempotency tuning that had ping-ponged across rounds 4→5.

### R6-01 · [HIGH][money] Refund per-payment ceiling was payment-wide but the reversal (batch Q) is invoice-scoped → a refund > the payment's share of the named invoice under-released → cash out with no restored receivable ✅ FIXED (batch S, `after 1530f21`)
- **Where:** [apps/finance/services/__init__.py:691](apps/finance/services/__init__.py#L691). The per-payment ceiling (allocated + in-flight) is now scoped to `(payment, invoice)`, matching exactly what `reverse_allocations_for_payment(payment_id, invoice_id)` releases. +test. (A regression from my own R5-01 fix — the round-5 re-review had approved R5-01; round 6 caught the ceiling mismatch it left.)

### R6-02 · [MEDIUM][money] Absence-deduction approve-time re-check ignored the excused-only policy → an EXCUSED→ABSENT excuse-revocation before approval let an excused-only center credit an unexcused absence ✅ FIXED (batch S)
- **Where:** [apps/approvals/services/__init__.py](apps/approvals/services/__init__.py) `_apply_absence_deduction_effect`. Approve now re-enforces `absence_deduction_excused_only` (mirrors create-time). +test. (Gap in my batch-P R5-P1 fix.)

### Cash idempotency — resolved with a time window (ended the R4→R5→R6 flip-flop) ✅ FIXED (batch T)
- The three rounds each caught the previous heuristic: unique key → double-click double-credit (R5); amount-only key → silent loss of equal-amount installments (R6/PLAUS1+PLAUS2). A headerless cash POST now keys on `(invoice, cashier, amount, 60s bucket)` — a same-second resubmit coalesces, genuine later repeats record. The precise dedupe remains a **client `Idempotency-Key`** (the correct contract — a frontend-coordination note, not silently imposed).

### R6-04 · [LOW][contract] Payme `params.id` > 128 chars → `DataError` on the audit pre-record → HTTP 400, breaking the always-200 JSON-RPC contract ✅ FIXED (batch T)
- **Where:** [apps/payments/webhook_views.py:157](apps/payments/webhook_views.py#L157). Skip the pre-record for an oversized id; `client.handle` returns a proper JSON-RPC error at 200.

### R6/PLAUS3 · [LOW][injection] Audit CSV export wrote attacker User-Agent/actor_repr verbatim → spreadsheet formula injection (== prior #97 class) ✅ FIXED (batch T)
- **Where:** [apps/audit/views/v1/audit_views.py:133](apps/audit/views/v1/audit_views.py#L133). Added `_safe_cell` (apostrophe-prefix leading `= + - @ \t \r`), mirroring `reports.generators.safe_cell`. +test.

### R6-03 (aka R6/CONF3) · [MEDIUM][DoS] Every inbound webhook inserts a `WebhookEvent` row before the signature gate (attacker-controlled `event_id`), and batch-M (R4-02) removed the rate limit → unauthenticated unbounded storage flood ✅ FIXED (owner-approved, commit `005d53a`)
- **Where:** [apps/payments/webhook_views.py](apps/payments/webhook_views.py) + [celery_tasks/payment_tasks.py](celery_tasks/payment_tasks.py). Resolved the R4-02 money-path tension by throttling **only the invalid-signature path** per client IP (`WEBHOOK_INVALID_RATELIMIT=120/min`): a legitimate provider always signs correctly so its callbacks are NEVER counted/throttled (the money path can't be re-broken), while an attacker lacks the secret and so can only produce invalid-signature requests, which are capped — over budget the audit INSERT is skipped and the provider's normal bad-sign response is returned (Payme still gets JSON-RPC 200). Plus `prune_webhook_events` daily retention (90-day window) to bound long-term growth; a provider source-IP allowlist at the edge is recommended (ops) for the distributed case. Rejected webhooks are still recorded for audit up to the budget. +3 tests.

---

## Round 7 — focused money-path re-review (convergence check, 3 lenses)

3 raw → **1 CONFIRMED** (2 refuters split it MEDIUM/LOW; other 2 raw were the same finding). Critically, round 7 found **no defect in this session's newest money fixes** (the batch-S refund per-payment ceiling and the batch-T cash time-window both survived a full multi-payment/multi-invoice matrix re-review) — a strong convergence signal that the money path is now consistent.

### R7-01 · [MEDIUM→LOW][money] `refund_payment` treated an explicit `amount=0` as a FULL refund (`0-is-falsy`) ✅ FIXED (batch U, `after report commit`)
- **Where:** [apps/payments/services/__init__.py:637](apps/payments/services/__init__.py#L637). `amount_uzs or payment.amount_uzs` — `bool(Decimal("0"))` is False, so `{"amount": 0}` became the full payment amount and bypassed `request_refund`'s `amount <= 0 → 400` guard, silently refunding the whole payment. Presence-check now (`None` → full; explicit `0` → 400). The `0-is-falsy`/presence-vs-truthiness class the project has fixed elsewhere. +test. Pre-existing (not one of this session's fixes).

---

## Summary of this session

**7 independent hunt rounds** (14 + 16 + 12 + 9 + 6 + 6 + 3 lens/area agents, each finding verified by 2 adversarial refuters), all disjoint from the prior 346-finding audit; rounds 5–7 **adversarially re-reviewed this session's own fixes** and caught incomplete edges + one HIGH regression (all closed) — the re-review loop is what keeps the fixes honest, and **round 7 converged** (no defect in the newest money fixes). **~60 new findings** surfaced. **22 gated fix batches** committed to `day1-build` (unpushed), each green on the affected apps + ruff + mypy; **9 full-suite checkpoints** all `0 failed` (1548 → … → 1583). Highlights fixed: HIGH — 2 Click/Uzum payment-loss (checkout/amount), reward self-dealing, webhook-retry payment-loss, reschedule-notify data-loss, cross-tenant file exfiltration, refund cross-invoice ledger corruption, rule bulk-reschedule cross-branch write. MEDIUM/LOW — cash double-payment loss, webhook throttling, report crash-recovery, statement DoS, pagination row-drop, stacked-discount/invoice integrity, IDOR scoping ×4, N+1/OOM, async fan-out, AI-placeholder fake grade, nightly-metering localdate, race/lock/idempotency defects.

**Owner-handed & implemented this session:** R2-04 (multi-cohort — implemented, see above; owner also has a parallel session on `task_a0fba4be`), R4/PLAUS1 (owning-read-permission gate landed; object-level redesign still tracked in `task_16ac6823`). **Still flagged for the owner (not silently guessed):** R3-P3 (AI idempotency — shared-infra/spend policy), R4-07 (duplicate nightly metering — ownership decision), R5-04 (datetime `+00:00` vs `Z` — canonical-format contract decision + broad sweep). **Deferred (need a coordinated change):** R2-13 (localdate sweep), R2-14/R2-15/R3-08/R3-P2/R1-10 (DB migrations — uniqueness constraints carry a deploy hazard if existing prod data already violates them, so they need an owner-aware data-cleanup step; a pure additive index like R2-15 is safe), R4-06 (iCal-token access-log redaction — deploy config), R3-07/R3-09/R3-P1 (LOW, subtle). **Backlog correction:** two `agents/FEATURE_BACKLOG.md` "TODO"s are already implemented — F5-5 (multi-branch scope works via the membership set + unscoped DIRECTOR) and A-1 notify-on-disburse (`approval.disbursed` exists).

**Not done (owner's call):** nothing was pushed/deployed — `origin/master` auto-deploys to a shared server, so that's the owner's decision. Load-testing "thousands of users" was not possible without a load environment; scale work here is code-level (N+1 elimination, row caps, pagination stability, lock-scope reduction) and is noted as such.

---

## Feature-completeness audit + build (pipeline step 4: "finish the product")

After the bug rounds converged, a **14-agent read-only audit** (6 mappers over FEATURE_LIST.md #1–24 + docs/PRODUCT_VISION.md, each gap re-verified by a 2nd agent) traced **44 features → 36 DONE, 8 confirmed gaps**. The product is overwhelmingly built; the gaps split into *safe-to-build* (additive, obviously-intended, no product decision) and *must-not-guess* (design/spec/infra decisions).

**BUILT this session (3 gaps — additive, gated, no behavior change to existing paths):**
- **F2 "remove from group"** — `POST /api/v1/cohorts/<pk>/remove-student/` unenrolls a student to *groupless* without moving them (history preserved; primary cohort recomputed from any surviving membership else cleared). Does NOT emit `cohort_member_moved` (its receivers key on a destination cohort a removal lacks). Commit `2d0599a`.
- **F4 co-teachers/assistants** — the `CohortTeacher` model was read everywhere but had no API write path (admin-only). Added `GET/POST /api/v1/cohorts/<pk>/teachers/` (assign = idempotent upsert) + `DELETE …/teachers/<teacher_id>/`. Commit `2d0599a`.
- **F10-1 dynamic SMS send date** — `Campaign.scheduled_at` (nullable+indexed, migration 0004) + `dispatch_due_campaigns` beat sweep (per-tenant fan-out, 5-min cadence) that auto-sends DRAFT campaigns once due, reusing the lock-claimed idempotent `send_campaign`. Null = manual send (unchanged). Commit `3b5101e`.

Each: reuses the app's existing scope/permission guards, tests added (17 new), per-app + celery-registration gates green, ruff + mypy clean.

**BUILT — the owner supplied the decisions, then approved building all five (each gated + tested):**
- **F13-1 dynamic salary/payout engine** ✅ (`e55f5a0`) — owner decision: pay is DYNAMIC + per-center configurable via API (not one formula). `teachers.PayoutPolicy` (method ∈ hourly / percent_of_collected_tuition / flat_monthly + params), `GET/PUT /teachers/<id>/payout-policy/`; `compute_payout` (taught lesson-hours × rate / Σ `PaymentAllocation` collected from the teacher's students × % / flat); `POST /teachers/<id>/prepare-salary/` computes it and raises an A-1 `salary_prep` request (computed amount, teacher pinned as payee) → manager approves → cashier disburses. Wired the previously-handlerless `salary_prep` kind + SoD (a teacher can't approve/disburse their own salary).
- **F8 reading/listening/speaking answer types** ✅ (`aa48651`) — added to `PlacementQuestion.QuestionType`, all human-marked (`HUMAN_GRADED_TYPES`); new `media` JSONField (audio/passage) returned in both the staff and taker views; the manual-mark path now scores all three like writing.
- **F8 mobile-only test creation** ✅ (`7356fea`) — `CenterSettings.placement_test_creation_mobile_only` + an `X-Client: mobile` gate on the placement authoring endpoints (a soft, spoofable policy gate, owner-confirmed).
- **F3 teacher-dashboard "forms to fill"** ✅ (`34a9b5e`) — `Form.audience_roles`/`audience_user_ids` targeting; the teacher dashboard returns `pending_forms` = published, open, targeted forms the teacher hasn't answered.
- **F12/F15 card↔attendance scan tie** ✅ (`99e9392`) — a valid door scan marks the student PRESENT on their active-cohort lesson within ±30 min; owner-confirmed money-safe rule: never overrides a teacher's mark, never creates an absence (only ADDs presence, which also stops the auto-absent sweep from penalising a scanned student).

**Conclusion:** the product is now feature-complete against FEATURE_LIST.md — all 5 previously-flagged gaps built + gated after the owner supplied the product decisions (the dynamic salary model, the media/grading approach, the mobile-gate mechanism, the forms-targeting model, and the money-safe scan rule).

**Adversarial self-review of the 3 newest features (money-path focus) — 4 defects caught + FIXED (`de55288`):** a 4-dim × 2-skeptic pass over `aa48651`/`99e9392`/`e55f5a0` (the earlier 3-feature self-review caught 2 defects; this one caught 4 in the money-touching pair, all real, 2 refuters each):
  - **F13-1 percent-of-tuition OVER-COUNT** [MED] — `compute_payout` summed `PaymentAllocation` by `student_id` only, so a student in another teacher's course credited THIS teacher for that other tuition → total payout could exceed tuition collected. Fixed: scoped per cohort the teacher teaches (`Invoice.cohort`).
  - **F13-1 never-500** [MED] — `period_end + 1 day` overflowed at `date.max` (`OverflowError`, unmapped → 500) on the salary endpoint. Fixed: caught → clean 400.
  - **F12/15 wrong-lesson** [MED] — `mark_present_from_scan` used `order_by(starts_at).first()`, so a back-to-back scan landed on the earlier (often already-ended) lesson, mis-feeding auto-absent/deduction. Fixed: prefers the in-session lesson, else the nearest start to the scan time.
  - **F13-1 authz bypass** [MED] — `salary_prep` was in the generic approvals `_REQUEST_KINDS`, letting an `approvals:write` user POST a raw, uncomputed, unscoped salary. Fixed: removed it — only the computed + branch-scoped `prepare_salary` path creates it.
  Each with a regression test. **Lesson reinforced: run the adversarial refuter pass on every new feature — the money-path ones especially. Both self-reviews this session found real defects the green per-app gate did not.**

**Branch audit (pipeline step 6 — "don't miss already-built work"):** `day1-build` is a strict superset of `origin/master` (the deploy target — 0 commits on master that day1-build lacks) and of the feature content of `origin/v1-starter` (the older 20-app pre-migration foundation; day1-build has 38 apps, and every file "absent" from day1-build is an old flat `serializers.py`/`services.py`/`views.py` deliberately removed by the off-DRF layered migration). The only other branch, `origin/dependabot/…`, is a CI-config bump. No already-built feature is stranded on another branch — `day1-build` is canonical.

**Adversarial self-review of the 3 new features (mandatory before trusting them):** a 4-dimension × 2-skeptic review (money-integrity / authz-scope / never-500 / concurrency) of commits `2d0599a`+`3b5101e` confirmed **2 real defects I introduced** (2 candidates were refuted by the 2nd skeptic and correctly dropped: a co-teacher teacher-branch check — acceptable, parity with `primary_teacher` + the assigner is already branch-scoped; and a `DoesNotExist`→500 in unenroll's re-fetch — a verbatim copy of the pre-existing `move_student` pattern, not reachable in-request). Both confirmed defects FIXED in commit `1308a11`:
  - **[MEDIUM] `current_cohort` NULL-strand race** — `enroll_student_in_cohort` didn't lock the student row, so a concurrent enroll-into-C + my new unenroll-from-A (the only writer of `current_cohort=NULL`) could strand a student ACTIVE-in-C but `current_cohort=None` (mis-targets segment SMS, shows groupless). Fixed: enroll now takes the same `select_for_update` lock as move/unenroll and re-reads the primary under it, serialising all three paths.
  - **[HIGH] campaign double-send** — `send_campaign`'s SMS loop runs outside the claim lock and `SENDING` is resumable, so my new auto-dispatch beat racing a manual "send now" (or a redelivered task) double-texted recipients (paid SMS + consent breach); the `dispatch_due_campaigns` docstring falsely asserted idempotency. Fixed: per-recipient compare-and-swap claim (`PENDING→SENT`) so each recipient's SMS fires at most once under concurrent invocations; docstring corrected. **Lesson: newly-written code needs the same adversarial refuter pass as the hunt findings — the review caught a HIGH money-adjacent defect the per-app gate (green) did not.**

---

## Scale-hardening audit (pipeline "harden for scale": N+1 / indexes / unbounded queries)

A 5-cluster read-path audit (each finding verified real AND safe-additive by a second agent) confirmed **13 scale defects** on hot read paths, all fixed additively in commit `3a6c43a`. One real finding (billing `usage_view` unbounded) was correctly **excluded** because its only safe fix changed the response envelope (not purely additive) — recorded, not forced.

**Missing index (8 — additive `AddIndex` migrations).** Every one is a growable table whose default list orders by `-created_at`/`-issued_at` with no covering index (existing composites lead with a filter column), forcing a full-table sort + top-N heapsort on each page-1 load at scale. Added `Index(("-created_at","id"))` (exact match for the `ORDER BY … DESC, id ASC LIMIT` the paginator emits) to: **students** `StudentProfile` (the hottest list), **approvals** `LedgerEntry` + `ApprovalRequest` (the money spine), **payments** `Payment`, **attendance** `AttendanceRecord` (fastest-growing: students×lessons), **printing** `PrintJob`, **compliance** `Penalty` (`-issued_at`), **staff_tasks** `Task`.

**N+1 (1).** `users.presenters.user_to_dict` filtered `role_memberships` with `.filter(revoked_at__isnull=True)`, which bypasses the list queryset's `prefetch_related` cache → a fresh query per user on the directory page (25–100 extra queries/page). Now filters the prefetched cache in Python (0 extra queries on the list path).

**Unbounded query (4).** (a) `academics` honor-roll/warnings materialized the entire matching Grade set into one response — hard-capped the top-N rows via a SQL-`LIMIT` slice. (b) `schedule` iCal feed served a director the whole tenant's lesson history rebuilt in memory on every calendar poll — bounded to a 90-day recent-past window + a 2000-row cap. (c) `campaigns` recipients endpoint returned the full frozen recipient set (one row per targeted student) — now paginated like every sibling list. (d) `messaging` thread list `prefetch_related("messages")` loaded **every** message of every listed thread just to count unread (a page of long threads = tens of thousands of rows for a few integers) — dropped the prefetch and replaced it with one bounded grouped query (`ThreadService.unread_counts`) reproducing the exact unread semantics.

All 13 reuse existing scope/permission guards, add no owner-facing behavior change beyond the deliberate feed-window/pagination bounds, and are covered by +4 regression tests; the 12 affected apps run **486 passed / 0 failed**, ruff + mypy clean.

---

## Deploy + live-verification findings

Deployed `day1-build`→`master` (auto-deploy: build → `migrate_schemas` → `up -d`). All 8 additive migrations applied OK across schemas; `/healthz/live` 200; a real tenant API route (`/api/v1/auth/login/`) returned 405 (correct, POST-only); Celery worker connected to the Redis broker. **Verifying the deploy surfaced one genuine latent bug:**

### DEPLOY-1 · [MEDIUM][config/ops] `settings.REDIS_URL` was never defined → `get_redis()` `AttributeError` → `/healthz/ready` always 503 + task DLQ push 500s ✅ FIXED (commit `cc33929`)
- **Where:** [config/settings/base.py](config/settings/base.py) / [infrastructure/cache/redis_client.py](infrastructure/cache/redis_client.py). `REDIS_URL` was only ever read as `env("REDIS_URL")` at the cache/broker/channel-layer config sites — never assigned as a settings attribute. But `get_redis()` reads `settings.REDIS_URL`, so on the REAL (unmocked) path it raised `AttributeError`: the readiness probe returned 503 "Cache unavailable" on the live server (even though the Django cache, broker, and a fresh redis client all worked), and the observability **dead-letter-queue** push (`get_redis().lpush`) 500'd on every failed task. Invisible to tests because the health unit tests mock `get_redis`. **Fix:** `REDIS_URL = env("REDIS_URL")` (the same call the cache/broker already use). +1 regression test on the real path. Pre-existing (not introduced this session) — the value of actually verifying the deploy.

---

## Remaining-items triage (after the owner approved sweeping the flagged list)

The owner authorized fixing everything + merge/push/deploy. **All MEDIUM+ findings are now FIXED** (the two security items R6/CONF3 + R1-05, plus R3-P3 this pass; everything else was fixed in the earlier rounds). What remains is either a genuine **decision** (which must not be guessed) or **LOW** edge-case concurrency whose fix carries more deploy risk than the bug:

**Genuine decisions (surfaced to the owner, not guessed):**
- **R4-07** [MEDIUM, scale] — two nightly beat tasks (`billing.meter_center` + `reports.aggregate_center`) both upsert `UsageSnapshot(center, today)`. Post-R4-05 they agree (no corruption) — it is a 2× nightly scan, not a bug. Both are documented + **tested** features (`test_beat_consolidation.py` asserts the beat entry; `test_reports.py` calls `aggregate_center`), so deduping cleanly needs the owner's call on whether platform-analytics aggregation should be independent of billing metering. Not worth breaking a documented feature to save one nightly scan.
- **R4-P1** [MEDIUM, IDOR] — print-job presign enforces the tenant prefix but not per-object ownership. Partially mitigated (owning-read-permission-per-source gate landed earlier). The full object-scope redesign (non-uniform S3 keys, cross-app object ownership) is intricate — tracked in `task_16ac6823`; warrants a supervised change, not an overnight guess.
- **R5-04** [MEDIUM, cosmetic] — datetime renders `+00:00` (string-preformatted presenters) vs `Z` (raw-datetime via DjangoJSONEncoder) for different fields. Both are valid ISO-8601; unifying is a **client-contract decision** (pick the canonical) + a broad response-shape sweep that risks breaking a client parsing one form. Needs the owner to pick `Z` or `+00:00`.
- **cash `Idempotency-Key`** — the current 60s-window fallback is correct; the precise dedupe is a client header contract (frontend coordination), not a server bug.

**LOW, deferred (fix risk > bug; each needs a dedupe-then-constrain migration or a coordinated sweep):**
- R1-10 / R2-14 / R3-08 / R3-P2 — add a uniqueness constraint (NotificationDelivery double-send; RoleMembership null-dept; open print-job; folder root-name). Each needs a Postgres partial/`nulls-distinct` constraint **plus** a data-migration to dedupe any existing violating rows first (deploy-safe). Batch as one migration pass under supervision.
- R2-13 — UTC-vs-local as-of-date needs a repo-wide `timezone.now().date()`→`localdate()` sweep kept consistent across attendance + membership dating (the isolated sites were already fixed in R4-05).
- R3-07 / R3-09 / R3-P1 — dept-head branch scope (ambiguous "head's branch"), k-anon distinct-count, transcript enumeration oracle.
- R4-06 — iCal token is a URL-path credential logged by the access log; mitigation is deploy-config (log redaction / disable the `?token=` fallback), not app code.

---

## Fault-isolation + response-consistency + security audit (2nd owner request, 2026-07-08)

Owner asked to keep hardening: per-app fault isolation, response-envelope consistency, security/passwords, static files, performance. Built the fault-isolation layer, then ran an 8-agent adversarial audit (response-consistency + security/passwords + authz/injection, each finding refuted by an independent verifier). 5 confirmed findings — all fixed + gated.

**Fault isolation (FEATURE, `819178e`):** `core/availability.py` (mount→app map, hard/soft dependency graph, per-tenant runtime disable set + global `DISABLED_APPS`) + `AppAvailabilityMiddleware` (a disabled app or one whose HARD dep is down → clean `503`; a SOFT dep down → served with a `warnings` list injected into the success envelope) + control endpoint `GET/PATCH /api/v1/org/system/apps/`. ONE app failing never falls the whole API. 6 tests.

**FI-1 (HIGH, error-envelope inconsistency) — FIXED.** The API shipped TWO error shapes: flat `{success:false,code,message}` (layered views, rate-limit) vs nested `{error:{code,detail}}` (Django `handler404/400/403/500`, `_jsonify` HTML-rewrite, `InactiveTenantMiddleware`, `AppAvailabilityMiddleware` 503, billing paywall 402, the DRF `reports` handler). A client couldn't branch on errors uniformly. Converged EVERY API-client-facing error path to the flat `core.responses.error()` shape (`core/middleware.py` `_error_envelope`/`center_inactive`/`service_unavailable`; `core/exceptions.py` `drf_exception_handler`; `apps/billing/middleware.py`). Deliberately LEFT: `/healthz/*` (separate ops-probe contract, not `/api/`), payments webhooks (provider-exact formats). 6 test assertions flipped nested→flat.

**FI-2 (MEDIUM, reports bare envelope) — FIXED.** `reports` (lone DRF app) returned bare serializer data + DRF `{count,results}` pagination and silently bypassed the availability warnings injection (no `success` key). Added `core.renderers.StandardEnvelopeRenderer` (wraps DRF responses into `{success,data,pagination}`, passes error bodies through) wired to the 3 reports viewsets. 5 renderer unit tests + 2 HTTP envelope tests.

**FI-3 (MEDIUM, audit detail bare) — FIXED.** `audit_detail_view` returned `JsonResponse(audit_to_dict(row))` (bare) unlike every other detail view → now `success(...)`.

**FI-4 (MEDIUM, reports datetime) — FIXED.** DRF localized `reports` timestamps to `Asia/Tashkent` (`+05:00`) while every layered presenter emits UTC (`+00:00`). Added `UtcDateTimeField` (forces UTC + plain isoformat) on the reports read serializers → whole API now emits `+00:00`. (Closes the long-deferred R5-04 datetime item.)

**FI-5 (MEDIUM, broken function-level authz) — FIXED.** `grade_recompute_view` resolved cohort/subject/term by raw pk with NO writable-cohort scope, so any `academics:write` holder (incl. a plain TEACHER) could recompute AND publish grades for a cohort they don't teach, cross-branch. Added the same `_writable_cohort_ids` gate the exam write-path uses (`403 forbidden` for a non-taught cohort). Regression test mirrors `test_teacher_cannot_create_exam_in_non_taught_cohort`.

**Static files (`de6b4b0`):** admin CSS 404'd under gunicorn (no static route in the urlconfs; gunicorn ≠ runserver). Added DEBUG-guarded `staticfiles_urlpatterns()` to both urlconfs (zero new dep; server runs DEBUG=True). Verified live: `/static/admin/css/base.css` → 200 (23,100 bytes) on both tenant (`demo.localhost`) and public (`localhost`) schemas. **Security agent: 0 password/secret-exposure findings** (the `security-passwords-secrets` dimension returned empty — no leaked secrets, no plaintext passwords, hashing intact).

---

## Performance / load test (test server, owner request)

Load-tested the live test server (internal `:8011`, gunicorn direct) with a stdlib load tester. **The app is fast per-request; the box is the ceiling.**

**Uncontended (c=1) p50 latency — genuinely fast:** `/healthz/ready` 24 ms · `/api/v1/users/me/` 50 ms · `/api/v1/students/` (paginated) 63 ms. Zero 5xx under every burst up to c=50 — nothing "fell."

**Under concurrency, throughput plateaued at ~34–37 req/s** (c=25 health p50 600 ms; c=50 p50 1.3 s). The box is **1 vCPU** (`nproc: 1`, load-avg 2.04) and during a c=25 burst `starforge-web-1` 41% + `starforge-postgres-1` 34% CPU saturated the single core — but that 34% postgres was **mostly per-request connection churn, not query work** (see PERF-1). The shared box also hosts redis/minio/daphne/celery + the separate `pos_control` + `caddy`.

**Worker tuning does NOT move the ceiling (proven, then reverted).** With owner approval, tried `--workers 3 --threads 4 --worker-class gthread`: throughput stayed ~35 req/s and tail latency got *worse* (c=50 p99 3391 ms vs 1775 ms) — because you can't exceed 1 core of CPU by adding workers; they just add context-switch overhead + RAM. **Reverted to the original `--workers 2`** (leaner, better tails, same ceiling); server restored to its pre-test state (backup kept).

**PERF-1 (app-level, FIXED — the dominant *fixable* bottleneck): `CONN_MAX_AGE` was unset (=0)** → Django opened+closed a fresh Postgres connection every request; with django-tenants that also re-runs `SET search_path`, burning DB CPU under load (the 34% postgres figure above was almost entirely connection churn, not the trivial `SELECT 1`). Set `CONN_MAX_AGE=60` (env-overridable) + `CONN_HEALTH_CHECKS=True` in base settings (Django 4.1+ detects a dead connection at request start); pinned `CONN_MAX_AGE=0` in test settings so pytest's per-test transactions + schema switching stay isolated.

**Measured effect (same 1-vCPU box, clean c=25 health burst, deploy `ede0869`):** per-request health **24 ms → 5.5 ms**; throughput **~34 → 155 req/s (~4.5×)**; p50 latency **~600 ms → 140 ms**; **postgres CPU 34% → 6.9%** (~5× less). This also explains why the worker experiment failed — the bottleneck was a *serial* per-request DB-connect cost, not worker concurrency, so `--threads`/`--workers` couldn't touch it but connection reuse did. The 1 vCPU still caps *absolute* throughput, but the app now uses it ~4.5× more efficiently. Permanent win on any box.

**Recommendations to actually raise the ceiling (infra, owner's call):** (1) **more vCPUs** — this is a 1-core box; that's the dominant lever; (2) run production under `DEBUG=False` (the test server runs `config.settings.development`, DEBUG=True, which adds per-request SQL-capture + CPU overhead); (3) fewer co-tenants on the box, or a dedicated instance.

---

## Adversarial hunt on this session's new code (6-dimension, 2-refuter)

Re-ran the loop on the code ADDED this session (fault-isolation, envelope convergence, renderer, `CONN_MAX_AGE`, the 5 features) — 6 finder dimensions × 2 independent refuters (≥2 real = confirmed). 4 confirmed findings reduced to **2 distinct real bugs** (both in my fault-isolation code), both fixed. **Key negative result: the `CONN_MAX_AGE` + django-tenants dimension found `{}` — no cross-tenant `search_path` leak** (django-tenants sets the schema per request on the reused connection), so the persistent-connection change is adversarially confirmed safe.

**HUNT-1 (HIGH, self-lockout) — FIXED.** `set_tenant_disabled_apps` let a director disable ANY known app including `org`/`auth`/`users`. Since the availability control endpoint itself lives under `/api/v1/org/`, PATCHing `{"disabled":["org"]}` 503'd the very endpoint needed to re-enable it — a permanent, API-unrecoverable tenant lockout (found independently by 3 dimensions). Added `PROTECTED_APPS = {auth, users, org}`: `set_tenant_disabled_apps` strips them, `resolve_status` never marks a protected app disabled (belt-and-suspenders vs a stale/global entry), and the control view rejects a PATCH naming one with a clear 400. +test.

**HUNT-2 (HIGH→MED, N+1 Redis reads) — FIXED.** `resolve_status()` called `disabled_apps()` (a Redis `GET`) at EVERY node of the dependency-graph walk, so one `/api/v1` request paid several serial Redis reads (the module docstring claimed "one cache read"), and `system_status()` multiplied it across ~38 apps — eroding the `CONN_MAX_AGE` win on the hot path. Refactored to read the disabled set ONCE per call and thread it through a pure `_resolve(app, disabled, seen)` recursion; `system_status()` reads once for all apps. +test asserting exactly one read for a multi-node chain.

**Refuted (correctly did not reach the ≥2 bar):** co-teacher payout "over-count" (intentional per-teacher policy), `compute_payout` Python lesson-loop (bounded + correct), 503-before-auth (intentional fault-isolation), `_classify` `api_error` vs stable codes on the DRF reports app (1/2 — rare exceptions, minor).

### Round 2 (money engine · concurrency/races · never-500 fuzz)

A second round targeting fresh high-stakes ground. **concurrency/races and never-500 came back dry** (no confirmed defect — the money POSTs use `select_for_update` inside `@transaction.atomic`, the campaigns/enroll compare-and-swap claims hold, and the write paths validate before hitting the DB). One confirmed money defect (2/2 refuters):

**MONEY-1 (MEDIUM, SoD bypass in the `reward` kind) — FIXED.** `reward` was in `_REQUEST_KINDS`, so the generic `POST /approvals/` accepted `kind="reward"` with a raw client payload — but `create_request` has no reward validation branch, so `recipient_id` (the self-dealing identity) and `party_label` (the immutable ledger payee) flowed through decoupled and un-coerced. The guard `_assert_not_beneficiary_self_dealing` compared `payload["recipient_id"] == actor.id`, bypassable two ways: **decoupling** (`recipient_id=999`, `party_label="pay me"` → guard checks the wrong identity) and **type confusion** (`recipient_id="5"` ≠ int `5`). A cashier holding `approvals:disburse` could thus approve+disburse their own cash reward. Fix (mirrors the `salary_prep` exclusion): **removed `reward` from `_REQUEST_KINDS`** so only `apps.rewards.grant_reward` can mint it (it pins `recipient_id` as int + derives `party_label` from the same recipient), **plus** hardened `_assert_not_beneficiary_self_dealing` to int-coerce the pinned id (defense-in-depth so a string can't slip past for ANY kind). +2 tests (endpoint rejects `kind=reward` 400; the guard blocks a string `recipient_id` matching the actor and never crashes on garbage). This was a pre-existing gap, not introduced this session.

### Round 3 (money-OUT SoD completeness · authz/IDOR · cross-tenant · websocket authz)

Money-OUT SoD completeness and cross-tenant isolation came back **dry** (the SoD map is complete for the money-OUT-to-staff kinds; no unscoped cross-tenant query or non-schema-scoped cache key). 3 confirmed defects — all pre-existing, all fixed:

**CONTENT-1 (MEDIUM, object-level authz / IDOR) — FIXED.** Content course/module/lesson/folder create + reparent checked only parent-FK *existence*, not that the target `ContentLibrary` was *visible* to the caller. Reads are visibility-scoped (`library__in=scoped_libraries`) but writes weren't — so a `content:write` holder (teacher/librarian, visibility-limited) could POST/reparent content into a DEPARTMENT/COHORT/ROLE-restricted library they can't see, surfacing it to that library's members (cross-department/cohort injection). The sibling `LibraryMaterialService.is_writable_library` + `content_upload_url_view` already guard this — an inconsistent omission. Fix: threaded `actor`/`roles` through the 4 CRUD services' `create`/`update` (default `None` = trusted internal call) and added `_assert_library_writable` (resolves the parent up to its library and checks `scoped_libraries`, raising `library_out_of_scope`), wired from `_crud_collection`/`_crud_detail`. +test (out-of-scope library → 403, in-scope → 201).

**WS-1 (MEDIUM, websocket authz) — FIXED.** `AttendanceConsumer._can_watch_cohort` gated the cohort-WIDE live feed on mere `attendance:read` + branch membership — but STUDENT/PARENT hold `attendance:read` row-scoped to self/children (HTTP selectors enforce that). So a same-branch student/parent received every peer's live attendance. Fix: gate on a DASHBOARD role (`{HEAD_OF_DEPT, TEACHER}` in the cohort's branch, or DIRECTOR sees all), excluding the row-scoped student/parent. +test (same-branch student → 4403); the re-auth heartbeat inherits the same gate.

**WS-2 (LOW, websocket staleness) — FIXED.** `NotificationConsumer` joined per-branch broadcast groups at connect but never overrode `_still_authorized`, so a branch membership revoked mid-session kept receiving that branch's announcements for the life of the socket. Fix: override `_still_authorized` to reconcile the branch-group set each heartbeat (leave revoked branches, join newly-granted ones) while keeping the socket + per-user feed — mirroring `AttendanceConsumer`'s re-check pattern.

**Session bug tally: 6 real bugs across 3 hunt rounds** (fault-isolation lockout + N+1, reward SoD bypass, content-write IDOR, 2 websocket authz). Rounds 1–3 each found real defects; the dry dimensions (CONN_MAX_AGE tenancy, concurrency/races, never-500, money-OUT completeness, cross-tenant) bound the converged surface.

### Round 4 (broad authz/IDOR across app-slices · write-scope asymmetry · privilege escalation)

3 of 4 dimensions **dry** — the ops/misc apps (tasks/messaging/rewards/loans/meetings/forms/placement/cards/…), the generalized read-scoped-but-write-unscoped sweep, and privilege escalation all clean. 1 confirmed defect (2/2 refuters):

**SCHED-1 (HIGH, object-level authz / IDOR) — FIXED.** `lesson_cancel_view` + `lesson_move_view` mutated a lesson resolved by raw pk with **no branch-scope assertion**. `scoped_lessons()` returns EVERY lesson tenant-wide for STAFF_ROLES `{DIRECTOR, HEAD_OF_DEPT, REGISTRAR, IT}`, but `core.scoping.is_unscoped` treats only superuser/DIRECTOR as unscoped — so a **branch-scoped HEAD_OF_DEPT/REGISTRAR** (both hold `schedule:*`) could POST `/api/v1/schedule/lessons/<pk>/cancel|move/` on **another branch's** lesson, rescheduling/cancelling that class and blasting `lesson_rescheduled`/`lesson_cancelled` to its cohort. The sibling `rule_detail_view` / `rule_bulk_reschedule_view` in the same file already guard this with `assert_branch_id_in_scope(request, rule.cohort.branch_id)` — the lesson write path was the lone omission. Fix: added `assert_branch_id_in_scope(request, lesson.cohort.branch_id)` after `get_scoped` in both mutating views (`lesson.cohort` is already `select_related`), before the body is parsed. Reads stay staff-broad, matching the `rule_*` read behavior. +test (cross-branch cancel/move → 403 `out_of_scope`; own-branch cancel → 200).

**Session bug tally: 7 real bugs across 4 hunt rounds.** The recurring seam was AUTHORIZATION (fault-isolation lockout, reward SoD, content-write IDOR, 2 websocket authz, schedule cross-branch write); money-math, tenancy isolation, input-fuzz (never-500), concurrency/races, and privilege-escalation dimensions all came back dry. Round 4's 3/4-dry result signals the authz seam is largely swept.

### Round 5 (systematic scope-guard consistency + side-effect scope) — convergence

The final tail-sweep. Both **guard-consistency slices (money/admin apps + academic/ops apps) came back DRY** — a systematic per-endpoint check for the exact CONTENT-1/SCHED-1 bug class (a write missing the scope guard its siblings/read-selector imply) found nothing. The side-effect-scope dimension found 1 LOW (2/2 refuters):

**NOTIF-1 (LOW, side-effect-scope IDOR) — FIXED.** `announcement_view` → `announce_cohort` fanned a notification out to every member of a client-supplied `cohort_id` with no branch-scope guard on the actor. **Not exploitable under the default catalog** (only unscoped DIRECTOR holds `notifications:write`), so LOW — but a defense-in-depth gap the moment a center A-2-grants `notifications:write` to a branch-scoped role (registrar/HOD): that role could then blast another branch's cohort. Every sibling broadcast/student-write (achievements grant, cards issue, sales) already guards branch scope specifically to stay robust to such A-2 grants; this was the lone omission. Fix: branch-scope the target cohort in `announcement_view` (`if not is_unscoped(request) and cohort.branch_id not in branch_ids(request): 403 branch_out_of_scope`) — no-op for a director, and a non-existent cohort returns the same 403 (no existence oracle). +test (A-2-grant a scoped role → cross-branch 403, own-branch 202).

**FINAL SESSION TALLY: 8 real bugs across 5 hunt rounds, every one AUTHORIZATION-related** (fault-isolation self-lockout + N+1, reward SoD bypass, content-write IDOR, 2 websocket authz leaks, schedule cross-branch lesson write, notification cross-branch blast). All fixed, gated, deployed, adversarially self-reviewed. Every non-authz dimension (money-math/SoD-completeness, tenancy isolation, `CONN_MAX_AGE` cross-tenant, never-500 input-fuzz, concurrency/races, privilege-escalation, write-scope-asymmetry, guard-consistency) came back dry — the authorization seam is now swept and the surface has re-converged.

---

## OpenAPI/Swagger + a DEBUG response bug (2026-07-09, after the schema/docs deliverable)

Built a full OpenAPI generator + rewrote the API contract (commit `5027ceb`), then hunted the new surface.

**HTTP2-1 (HIGH, site-down on the DEBUG test server) — FIXED (`4aad1fd`).** The owner reported `ERR_HTTP2_PROTOCOL_ERROR` / no response on `/` and any "normal" (unmatched) path. Cause: under `DEBUG=True`, Django serves an HTML technical-404/500 page (~3 KB); `CommonMiddleware` stamps `Content-Length` from that HTML, then `JsonErrorResponseMiddleware._jsonify` rewrites the body to the ~73-byte JSON envelope + resets Content-Type **but left the stale `Content-Length: 3020`**. The response declared 3020 bytes and sent 73 → HTTP/2 aborts the stream (`INTERNAL_ERROR`), HTTP/1.1 clients hang. Only unmatched/error paths broke (real endpoints return `JsonResponse` with a correct length). Production (`DEBUG=False`) returns JSON via `handler404` so it was unaffected. Fix: re-stamp `Content-Length` after the rewrite (mirrors `AppAvailabilityMiddleware._inject_warnings`). +regression test (DEBUG=True unmatched URL → `Content-Length == len(content)`). Verified live: `/` now returns a clean JSON 404 over HTTP/2.

**OPENAPI-1 + OPENAPI-2 (HIGH, schema-correctness) — FIXED.** The custom `core/openapi.py` generator handled the 37 plain-view apps but botched the lone DRF app (`reports`): (1) `_route_of` read only `RoutePattern._route`, but DRF-router routes are `RegexPattern` (`._regex`) → every reports route collapsed onto `/api/v1/reports/`, dropping the detail/runs/schedules paths; (2) method introspection resolved a DRF viewset's `as_view()` closure to `APIView.dispatch` (no `request.method` literals) → defaulted to GET-only, dropping create/update. Fix: `_route_of` translates a `RegexPattern` (named groups → `{name}`, skips `.json`/`.api` format-suffix routes), `_methods_and_meta` reads `callback.actions` for DRF viewsets (filtered by `http_method_names`), and `_build_paths` skips the DRF api-root. Reports now renders correctly (runs/schedules list+create+detail, library list+detail; +4 paths → 310). +regression test. The endpoints always worked — only the schema was wrong.

---

## Appendix A — Refuted candidates

_Populated on completion. Recorded so future passes don't re-investigate them._
