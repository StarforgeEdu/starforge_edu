# DAY 2 — Academic Engine

At start of day: DB migrated and seeded, CI green with coverage ≥70%, JWT tenant-bound (TD-1), fail-closed per-action perms (TD-4/5), `StudentProfile`/`TeacherProfile`/`ParentProfile`+`Guardian`, `Cohort`+membership, `org.Room`/`Holiday`/operating hours, `CenterSettings` (TD-13), and the two-tenant test fixture all exist (Day 1).
At EOD: a school can build a recurring timetable with conflict rejection, mark attendance (with auto-absent), run exams → grades → transcript PDFs, assign and collect homework with S3 attachments, and manage a content library with a full signed-URL upload/validate/download flow. All of it permission-gated, tenant-isolated, and tested.

Merge order today: **A → B → C → D → E → F** (ROADMAP §2.3). B FKs `schedule.Lesson` (A); C FKs `schedule.Term` (A); E FKs `academics.Subject` (C). Do not reorder.

---

## Prerequisites (verify before writing code)

Read the last 2 days of `agents/WORKLOG.md`. Day 1 may have landed with renames — the names below are the planned ones; **the WORKLOG entry wins** if it differs.

| Must exist (Day 1 lane) | Verify with |
|---|---|
| Migrations + seeded `demo` tenant (D1-A) | `uv run pytest -q` green on master; `uv run python manage.py showmigrations` shows no pending |
| Per-action perms: `required_perms: dict[action, "resource:verb"]`, fail-closed (D1-C, TD-4/5) | grep `required_perms` in `core/permissions.py`; a viewset with no mapping returns 403 in D1 tests |
| `apps.students.models.StudentProfile`, `apps.teachers.models.TeacherProfile`, `ParentProfile`+`Guardian` in `apps/parents` (D1-D) | `uv run python manage.py shell -c "from apps.students.models import StudentProfile"` |
| `apps.cohorts.models.Cohort` + membership model (D1-D) | same shell import |
| `apps.org.models.Room`, `Holiday` (national + per-branch), operating hours (D1-F) | same shell import |
| `CenterSettings` singleton (TD-13, D1-B) — import path per D1-B WORKLOG entry; assumed `apps.org.models.CenterSettings` below, substitute the real path everywhere | shell import + confirm the accessor (e.g. `CenterSettings.get()` / `get_solo()`) |
| Factories + two-tenant fixture + tenant-isolation test (D1-E, §26) | locate `conftest.py`; `uv run pytest -q -k isolation` green |

If any prerequisite is missing: log it in WORKLOG, build the smallest stub that unblocks you **in your own app** (never edit another lane's app), and bold the deviation in your handoff notes.

Shared-file discipline (ROADMAP §2.1): `core/permissions.py` (matrix rows), `config/settings/base.py` (knob defaults, `CELERY_BEAT_SCHEDULE`), `pyproject.toml` (TD-16 deps) take **append-only** edits. Rebase on master before pushing.

Celery convention (existing): task modules live in the top-level `celery_tasks/` package (autodiscovered by `config/celery.py` — `app.autodiscover_tasks(["celery_tasks"])`). Periodic tasks that touch tenant data are **fan-out tasks**: a public-schema task iterates active `Center`s and enqueues a per-tenant task via `tenant_schemas_celery` (`_schema_name=center.schema_name`). Task bodies call functions in `apps/<app>/services.py` — no business logic in task files.

---

## Lane A — Schedule (apps/schedule)

**Objective:** Replace `ScheduleItem` with materialized-occurrence scheduling per TD-12: recurrence rules expand to concrete `Lesson` rows, conflicts are detected on rows (plus DB exclusion constraints), one-offs detach, terms bound and archive, iCal feeds work, lesson reminders are emitted.
**Implements:** TASKS §9 (all items), §22 (`archive_completed_terms` task body), TD-12, TD-13, TD-16 (`python-dateutil`, `icalendar`).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D2-A-1 | Models `Term`, `TimeSlot`, `RecurrenceRule`, `Lesson` (fields below) + migration incl. `BtreeGistExtension` + `ExclusionConstraint`s | `apps/schedule/models.py`, `apps/schedule/migrations/`, `apps/schedule/admin.py` | `migrate_schemas` clean on fresh DB; inserting two overlapping `scheduled` lessons for the same room/teacher/cohort raises `IntegrityError` at the DB level | D1 models |
| D2-A-2 | `materialize_rule(rule, *, window=None)` service using `dateutil.rrule.rrulestr`; skips `org.Holiday` dates effective for the cohort's branch; transactional; re-run deletes+recreates only **future, non-detached, attendance-free** lessons | `apps/schedule/services.py` | Rule "Mon/Wed 14:00–15:30 for 4 weeks" with one holiday Monday yields 7 lessons; calling twice yields identical rows (idempotent); detached lesson untouched on re-materialize | D2-A-1 |
| D2-A-3 | `check_conflicts(...)` range-overlap selector (`starts_at__lt=ends, ends_at__gt=starts`, status=`scheduled`, same room/teacher/cohort); rule create/update and lesson move call it and reject with 409 `schedule_conflict` listing conflicting lesson IDs in `error.fields` | `apps/schedule/services.py`, `apps/schedule/selectors.py`, `core/exceptions.py` (only if a 409 base class is missing — additive) | POST a rule overlapping an existing teacher's lesson → 409 envelope (TD-18) with conflicting IDs; non-overlapping adjacent (14:00–15:00 then 15:00–16:00) is accepted | D2-A-2 |
| D2-A-4 | One-off ops: `cancel_occurrence(lesson, reason, actor)`, `move_occurrence(lesson, new_start, new_end, actor)` (sets `detached_from_rule=True`), `bulk_reschedule(rule, shift_minutes)` (all-or-nothing, conflict-checked) | `apps/schedule/services.py`, `apps/schedule/views.py` | Cancel sets status=`cancelled`+reason, emits `lesson_cancelled`; move conflict-checks and detaches; bulk shift of a week either moves every future lesson or none (assert rollback on induced conflict) | D2-A-3 |
| D2-A-5 | Viewsets + routers: `terms`, `timeslots`, `rules`, `lessons` with per-action `required_perms`, django-filter (cohort, teacher, room, `starts_at` range, status), ordering, `@extend_schema` | `apps/schedule/views.py`, `serializers.py`, `urls.py`, `selectors.py` | Endpoints below respond as specified; `lessons` list filtered by `?cohort=&date_from=&date_to=`; schema renders in swagger-ui | D2-A-1..4 |
| D2-A-6 | iCal feed: signed-token URL per user (no model — `django.core.signing`, salt `"schedule.ical"`, payload `{user_id, schema}`); feed view is AllowAny, rejects token whose `schema` ≠ `connection.schema_name`, serves `text/calendar` built with `icalendar` (teacher → taught lessons; student/parent → cohort lessons) | `apps/schedule/views.py`, `services.py`, `urls.py` | Feed round-trips through `icalendar.Calendar.from_ical`; token minted on tenant A → 401 `tenant_mismatch` on tenant B; cancelled lessons carry `STATUS:CANCELLED` | D2-A-5 |
| D2-A-7 | Celery: `send_lesson_reminders` (fan-out; per tenant select `scheduled` lessons with `starts_at` in [now+25m, now+35m] and `reminder_sent_at IS NULL`, emit `lesson_reminder_due` per lesson, stamp `reminder_sent_at`); `archive_completed_terms` (lessons in ended terms → status `archived`). Beat entries appended to `CELERY_BEAT_SCHEDULE` in `config/settings/base.py` (create the dict if absent): reminders every 5 min, archival weekly. D4-F consolidates. | `celery_tasks/schedule_tasks.py`, `apps/schedule/signals.py`, `config/settings/base.py` | Running the reminder task twice emits each signal exactly once (stamp = idempotency key, DoD #9); task runs under the correct tenant schema (asserted in test); **no SMS/push adapter imported anywhere in apps/schedule** — emit-only, D3-C wires dispatch | D2-A-2 |
| D2-A-8 | Matrix rows (append-only): `REGISTRAR += {"schedule:*"}`; confirm TEACHER/STUDENT/PARENT keep `schedule:read`. Tick TASKS §9. | `core/permissions.py` | F-lane parameterized matrix test passes for the `schedule` resource | — |

**Models (de facto schema — Lanes B/C consume these):**
- `Term`: `name: Char(100)`, `academic_year: Char(9)` (e.g. `2026-2027`), `start_date: Date`, `end_date: Date`, `is_current: Bool default False`; `UniqueConstraint(academic_year, name)`; `CheckConstraint(end_date > start_date)`; ordering `-start_date`.
- `TimeSlot`: `branch: FK org.Branch CASCADE`, `name: Char(50)`, `start_time: Time`, `end_time: Time`, `order: PositiveSmallInt`; `UniqueConstraint(branch, name)`; `CheckConstraint(end_time > start_time)`.
- `RecurrenceRule`: `term: FK Term PROTECT`, `cohort: FK cohorts.Cohort PROTECT`, `teacher: FK teachers.TeacherProfile PROTECT`, `room: FK org.Room PROTECT null`, `title: Char(200)`, `rrule: Text` (RFC 5545 RRULE string, validated by parsing at clean()), `start_date: Date`, `end_date: Date` (clamped to term), `start_time: Time`, `end_time: Time`, `is_active: Bool default True`, `created_by: FK users.User SET_NULL null`, timestamps; `CheckConstraint`s on date and time ordering.
- `Lesson`: `rule: FK RecurrenceRule SET_NULL null`, `term: FK Term PROTECT`, `cohort: FK cohorts.Cohort PROTECT`, `teacher: FK teachers.TeacherProfile PROTECT`, `room: FK org.Room PROTECT null`, `title: Char(200)`, `starts_at: DateTime`, `ends_at: DateTime` (tz-aware, Asia/Tashkent source), `status: Char choices scheduled|cancelled|completed|archived default scheduled`, `detached_from_rule: Bool default False`, `cancel_reason: Char(255) blank`, `reminder_sent_at: DateTime null`, timestamps; indexes `(cohort, starts_at)`, `(teacher, starts_at)`, `(room, starts_at)`, `status`; `CheckConstraint(ends_at > starts_at)`; three `ExclusionConstraint`s (btree_gist) over `tstzrange(starts_at, ends_at)` — equal `room` (where room not null), equal `teacher`, equal `cohort` — each conditioned on `status='scheduled'`.

**Endpoints:**
- `GET|POST|PATCH|DELETE /api/v1/schedule/terms/` — `schedule:read`/`schedule:write` per action — `{id, name, academic_year, start_date, end_date, is_current}`
- `GET|POST|PATCH|DELETE /api/v1/schedule/timeslots/` — `schedule:read`/`schedule:write`
- `GET|POST|PATCH|DELETE /api/v1/schedule/rules/` — `schedule:write` for mutations; create/update triggers materialization, returns 409 on conflict
- `GET /api/v1/schedule/lessons/` + `GET /api/v1/schedule/lessons/{id}/` — `schedule:read` — paginated `{id, title, cohort, teacher, room, starts_at, ends_at, status, detached_from_rule}`
- `POST /api/v1/schedule/lessons/{id}/cancel/` `{reason}` — `schedule:write` — 200 updated lesson
- `POST /api/v1/schedule/lessons/{id}/move/` `{starts_at, ends_at}` — `schedule:write` — 200 or 409
- `POST /api/v1/schedule/rules/{id}/bulk-reschedule/` `{shift_minutes}` — `schedule:write` — `{moved_count}` or 409
- `GET /api/v1/schedule/ical-url/` — any authenticated — `{url}`
- `GET /api/v1/schedule/ical/<token>/` — AllowAny (token-authed) — `text/calendar`

**Signals (in `apps/schedule/signals.py`, emit-only today):** `lesson_reminder_due(lesson)`, `lesson_cancelled(lesson, actor)`, `lesson_rescheduled(lesson, old_start, actor)`.

**Tests (`apps/schedule/tests/`, per agents/TESTING.md matrix):**
- `test_materialize_rule_counts_and_holiday_skip` — 8 slots minus 1 holiday = 7 lessons
- `test_materialize_idempotent` and `test_detached_lesson_survives_rematerialize`
- `test_conflict_room|teacher|cohort_overlap_409` + `test_adjacent_lessons_allowed`
- `test_exclusion_constraint_blocks_raw_orm_overlap` (IntegrityError, bypassing services)
- `test_bulk_reschedule_atomic_rollback_on_conflict`
- `test_ical_feed_valid_and_cross_tenant_rejected`
- `test_reminder_task_idempotent_and_schema_scoped`
- permission-denied per role, cross-tenant isolation per endpoint, query-count on `lessons` list

**Publish to WORKLOG:** `schedule.Lesson` + `schedule.Term` import paths and exact field names (B and C FK these); `check_conflicts` signature; signal names; whether you created `CELERY_BEAT_SCHEDULE`; the btree_gist migration (later lanes' fresh-DB runs need it).

---

## Lane B — Attendance (apps/attendance)

**Objective:** Replace `AttendanceItem` with `AttendanceRecord` keyed to `schedule.Lesson`; teacher-scoped marking with late threshold and correction window from `CenterSettings`; auto-absent beat task; guardian-absence signal; summaries, CSV export, cohort dashboard.
**Implements:** TASKS §10 (all items), §22 (`mark_absent_after_lesson` body), TD-13.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D2-B-1 | `AttendanceRecord` model (below) + migration | `apps/attendance/models.py`, `migrations/`, `admin.py` | Second record for same (student, lesson) violates the unique constraint; migration clean on fresh DB | D2-A-1 merged |
| D2-B-2 | `CenterSettings` knobs (add only those D1-B didn't): `late_threshold_minutes: int default 10`, `attendance_correction_hours: int default 24`, `auto_absent_after_minutes: int default 30` | per D1-B WORKLOG (assumed `apps/org/models.py`) + migration | Changing a knob alters behavior in tests with **no code change** (DoD #2) | D1-B |
| D2-B-3 | `mark_attendance(lesson, entries, actor)` service: upsert via `update_or_create`; validates actor is the lesson's teacher (director/head_of_dept bypass); validates each student has active membership in `lesson.cohort`; optional `arrived_at` ⇒ auto-`late` when `arrived_at > starts_at + late_threshold_minutes`; rejects edits later than `attendance_correction_hours` after `lesson.ends_at` with 403 `correction_window_expired` unless actor is director | `apps/attendance/services.py` | Teacher of another cohort → 403; student not in cohort → 422 field error; `arrived_at` 12 min late with threshold 10 → status `late`; frozen clock 25h after lesson → director succeeds, teacher gets `correction_window_expired` | D2-B-1/2 |
| D2-B-4 | Endpoints (below) with per-action `required_perms`, filters (student, lesson, `lesson__cohort`, status, date range), `@extend_schema`; `read_self`/`read_own_children` enforced by queryset scoping in `selectors.py` (student → own records; parent → records of `Guardian`-linked students) per the TD-5 mechanism D1-C published | `apps/attendance/views.py`, `serializers.py`, `urls.py`, `selectors.py` | Student lists only own records; parent only linked children's; teacher only their cohorts'; selectors `select_related("student__user", "lesson")` — query-count test green | D2-B-3 |
| D2-B-5 | Summary + dashboard selectors: per-student per-term `%present` (aggregate over `lesson__term`); cohort dashboard with per-student present/absent/late/excused counts + rate in a single aggregate query | `apps/attendance/selectors.py`, `views.py` | Summary math matches a hand-built fixture of 10 records; dashboard for a 30-student cohort executes ≤5 queries | D2-B-4 |
| D2-B-6 | CSV export `GET /api/v1/attendance/export/?cohort=&term=` streaming `text/csv` (date, lesson title, student, status, marked_by) — DB-only, sync is acceptable | `apps/attendance/views.py` | CSV row count == record count; gated `attendance:read`, teacher scoped to own cohorts | D2-B-4 |
| D2-B-7 | Celery `mark_absent_after_lesson`: fan-out over active Centers; per tenant, for each `scheduled` lesson with `starts_at <= now - auto_absent_after_minutes`, `get_or_create` `absent` records (`auto_marked=True`, `marked_by=None`) for active cohort members lacking a record; emit `student_marked_absent` per **created** record only. Beat entry: every 15 min (append to `CELERY_BEAT_SCHEDULE`). | `celery_tasks/attendance_tasks.py`, `apps/attendance/signals.py`, `config/settings/base.py` | Run twice → zero duplicate records, zero duplicate signals (`get_or_create` created-flag = idempotency); a student already marked `present` is never overwritten | D2-B-1/2 |
| D2-B-8 | `student_marked_absent(record)` also fires on manual `absent` marks (from D2-B-3). Emit-only; D3-C consumes for guardian SMS/in-app. Tick TASKS §10 + §22 `mark_absent_after_lesson`. | `apps/attendance/signals.py`, `services.py` | Signal asserted via test receiver; zero notification/SMS imports in apps/attendance | D2-B-3/7 |

**Model — `AttendanceRecord`:** `student: FK students.StudentProfile PROTECT`, `lesson: FK schedule.Lesson PROTECT`, `status: Char choices present|absent|late|excused`, `arrived_at: DateTime null`, `note: Char(500) blank`, `marked_by: FK users.User SET_NULL null`, `marked_at: DateTime auto_now`, `auto_marked: Bool default False`, `created_at`; `UniqueConstraint(student, lesson)`; indexes `(lesson)`, `(student, created_at)`, `status`; ordering `-created_at`.

**Endpoints:**
- `POST /api/v1/attendance/lessons/{lesson_id}/mark/` body `[{student, status, arrived_at?, note?}]` — `attendance:write`, teacher-scoped — 200 `{created, updated, records: [...]}`
- `GET /api/v1/attendance/records/` + `/{id}/` — `attendance:read` (read_self / read_own_children via selector scoping)
- `GET /api/v1/attendance/summary/?student=&term=` — `attendance:read` — `{present, absent, late, excused, percent_present}`
- `GET /api/v1/attendance/cohorts/{id}/dashboard/?date_from=&date_to=` — `attendance:read` — per-student rows + cohort rate
- `GET /api/v1/attendance/export/?cohort=&term=` — `attendance:read` — `text/csv`

**Tests (`apps/attendance/tests/`):**
- `test_mark_upserts_unique_per_student_lesson`
- `test_teacher_of_other_cohort_denied` / `test_student_not_in_cohort_rejected`
- `test_late_threshold_boundary` (exactly threshold = present; threshold+1 min = late)
- `test_correction_window_expired_teacher_403_director_ok` (time-machine, D2-F-5)
- `test_auto_absent_idempotent_double_run` / `test_auto_absent_skips_marked_students`
- `test_absence_signal_emitted_manual_and_auto`
- `test_summary_math` / `test_dashboard_query_budget`
- `test_csv_export_shape`, parent/student scoping, cross-tenant per endpoint, query-count on records list

**Publish to WORKLOG:** `student_marked_absent` signal signature (D3-C wires guardian notify); `AttendanceRecord` path (D4-C `AttendanceConsumer` + D4-B reports consume); the three knob names.

---

## Lane C — Academics (apps/academics)

**Objective:** Replace `AcademicsItem` with `Subject`/`Exam`/`ExamResult`/`Grade`/`Transcript`; per-Center grading scheme; weighted term grades; CSV bulk entry; grade-change audit signal; transcript PDFs via weasyprint→S3 (TD-14); honor roll/warnings; publication gating for parents.
**Implements:** TASKS §11 (all except AI exam generation — D4-A §18), TD-13, TD-14, TD-16 (`weasyprint`).

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D2-C-1 | Models (below) + migration | `apps/academics/models.py`, `migrations/`, `admin.py` | Fresh-DB migrate clean; duplicate (exam, student) result and duplicate (student, subject, term) grade rejected by the DB | D2-A-1 merged (Term) |
| D2-C-2 | Knobs: `grading_scheme: Char choices percentage|letter|gpa default percentage`, `honor_roll_min: Decimal default 90`, `academic_warning_max: Decimal default 60` (skip any D1-B already created) | per D1-B WORKLOG + migration | Switching scheme changes `Grade.value_display` for the same raw score with no code change | D1-B |
| D2-C-3 | Services: `record_results(exam, rows, actor)` (validates `0 ≤ score ≤ exam.max_score`, upserts, emits `grade_changed` with old/new on overwrite), `bulk_grade_import(exam, csv_file, actor)` (per-row errors, all-or-nothing transaction), `publish_exam(exam, actor)` | `apps/academics/services.py` | Score > max_score → 422 field error; CSV with 1 bad row of 20 → 422 listing row numbers, zero rows written; re-entering a score emits `grade_changed` exactly once | D2-C-1 |
| D2-C-4 | `compute_term_grade(student, subject, term)`: weighted mean of **published** exam results normalized to 0–100 (`Σ(score/max·weight)/Σweight`), writes `Grade` with `components` JSON breakdown + `value_display` per scheme (letter bands A≥90/B≥80/C≥70/D≥60/F; GPA = raw/25, 2dp); `recompute_cohort_term(cohort, subject, term)` wrapper | `apps/academics/services.py` | Hand-computed fixture (3 exams, weights .2/.3/.5) matches to 2dp; unpublished exam excluded; display mapping verified for all 3 schemes | D2-C-2/3 |
| D2-C-5 | Transcript: `POST /api/v1/academics/transcripts/` creates `Transcript(status=pending)` + enqueues Celery `generate_transcript_pdf(transcript_id)` → weasyprint renders `templates/documents/transcript.html` (gettext-wrapped; uz/ru/en via the student's `preferred_language`), uploads to S3 key `{schema_name}/transcripts/{transcript_id}.pdf`, status→`done`; `GET .../transcripts/{id}/` returns a signed `download_url` (`presign_download`, TTL 600) when done | `apps/academics/services.py`, `celery_tasks/academics_tasks.py`, `templates/documents/transcript.html`, `views.py` | POST → 202 `{id, status:"pending"}`; task idempotent (status=`done` short-circuits) and retries ≤3 with backoff; PDF bytes start `%PDF` in test (S3 stubbed); no weasyprint/S3 HTTP in any request handler (DoD #9) | D2-C-4 |
| D2-C-6 | Honor roll / warnings: selectors over published `Grade`s of the current term vs `honor_roll_min`/`academic_warning_max`; endpoints below | `apps/academics/selectors.py`, `views.py` | Student avg 92 with min 90 appears in honor roll; flipping knob to 95 removes them, no code change | D2-C-4 |
| D2-C-7 | Viewsets + publication gating: parent/student `Grade` selectors filter `is_published=True` and scope to self / Guardian-linked children; teachers see all for their cohorts. Per-action `required_perms`, filters, `@extend_schema`. Matrix rows (append-only): `TEACHER += {"academics:read"}` (currently has only `academics:write` — that asymmetry is a Day-1 leftover). Tick TASKS §11. | `apps/academics/views.py`, `serializers.py`, `urls.py`, `selectors.py`, `core/permissions.py` | Parent of student X: unpublished grade invisible, published visible, other students' grades never visible; teacher GET on grades now allowed (matrix test) | D2-C-3..6 |

**Models:**
- `Subject`: `name: Char(200)`, `code: Slug(50) unique`, `department: FK org.Department SET_NULL null`, `description: Text blank`, `is_active: Bool default True`; ordering `name`.
- `Exam`: `subject: FK Subject PROTECT`, `cohort: FK cohorts.Cohort PROTECT`, `term: FK schedule.Term PROTECT`, `type: Char choices midterm|final|quiz|project|oral`, `title: Char(200)`, `exam_date: Date`, `max_score: Decimal(6,2) default 100`, `weight: Decimal(4,3) default 1.0`, `is_published: Bool default False`, `published_at: DateTime null`, `created_by: FK users.User SET_NULL null`, timestamps; indexes `(cohort, term)`, `(subject, term)`; `CheckConstraint(max_score > 0)`, `CheckConstraint(weight > 0)`.
- `ExamResult`: `exam: FK Exam CASCADE`, `student: FK students.StudentProfile PROTECT`, `score: Decimal(6,2)`, `note: Char(255) blank`, `graded_by: FK users.User SET_NULL null`, `graded_at: DateTime auto`; `UniqueConstraint(exam, student)`; `CheckConstraint(score >= 0)`.
- `Grade`: `student: FK students.StudentProfile PROTECT`, `subject: FK Subject PROTECT`, `term: FK schedule.Term PROTECT`, `value_raw: Decimal(6,3)`, `value_display: Char(8)`, `components: JSON default list`, `is_published: Bool default False`, `published_at: DateTime null`, `computed_at: DateTime`; `UniqueConstraint(student, subject, term)`; index `(student, term)`.
- `Transcript`: `student: FK students.StudentProfile PROTECT`, `term: FK schedule.Term PROTECT null` (null = full history), `status: Char choices pending|processing|done|failed default pending`, `pdf_key: Char(512) blank`, `error: Text blank`, `requested_by: FK users.User SET_NULL null`, `generated_at: DateTime null`, `created_at`.

**Endpoints:**
- `CRUD /api/v1/academics/subjects/` and `.../exams/` — `academics:read`/`academics:write` per action
- `GET|POST /api/v1/academics/exams/{id}/results/` — `academics:write` for POST — bulk rows `[{student, score, note?}]`
- `POST /api/v1/academics/exams/{id}/results/import-csv/` — `academics:write` — multipart; 422 lists bad row numbers
- `POST /api/v1/academics/exams/{id}/publish/` — `academics:write`
- `GET /api/v1/academics/grades/?student=&subject=&term=` — `academics:read` (read_self / read_own_children scoping + publication gate)
- `POST /api/v1/academics/grades/recompute/` `{cohort, subject, term}` — `academics:write`
- `POST /api/v1/academics/transcripts/` + `GET .../transcripts/{id}/` — `academics:read` self-scoped; requesting for another student requires `academics:write` — `{id, status, download_url?}`
- `GET /api/v1/academics/honor-roll/?term=` and `.../warnings/?term=` — `academics:read` (staff roles per matrix)

**Signals/Celery:** `grade_changed(instance, old_score, new_score, actor)` in `apps/academics/signals.py` — emit-only, D3-D audit consumes (TD-9 lists Grade + ExamResult). Celery `generate_transcript_pdf` in `celery_tasks/academics_tasks.py` — idempotent per D2-C-5, runs under tenant schema, retries ≤3 exponential.

**Tests (`apps/academics/tests/`):**
- `test_weighted_term_grade_fixture` / `test_unpublished_exam_excluded`
- `test_value_display_percentage_letter_gpa` (3 schemes, knob-driven)
- `test_publication_gating_parent_student_teacher`
- `test_csv_import_atomic_and_row_errors`
- `test_transcript_task_lifecycle_idempotent` (pending→done, S3 stub asserted, `%PDF` magic)
- `test_grade_changed_emitted_once_on_overwrite`
- `test_honor_roll_knob_flip`, cross-tenant per endpoint, query-count on grades + exams lists

**Publish to WORKLOG:** `academics.Subject` path (D2-E FKs it — E merges after you); `grade_changed` signature (D3-D audit); transcript task name + S3 key scheme (D4-D printing reuses the PDF pattern); knob names.

---

## Lane D — Assignments (apps/assignments)

**Objective:** Replace `AssignmentsItem` with `Assignment`/`Submission`/`SubmissionGrade`; S3 attachments via presign; late flag + grace and resubmit limit from `CenterSettings`; rubric grading; plagiarism stub; AI-feedback request signal (consumed D4-A); created/due-soon/graded notification signals.
**Implements:** TASKS §12 (all; plagiarism = stub per spec; real AI feedback is D4-A §18), TD-13.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D2-D-1 | Models (below) + migration | `apps/assignments/models.py`, `migrations/`, `admin.py` | Fresh-DB migrate clean; duplicate (assignment, student, attempt_number) rejected | D1 models |
| D2-D-2 | Knobs: `assignment_grace_minutes: int default 0`, `assignment_max_resubmits: int default 2` (skip if D1-B made them) | per D1-B WORKLOG + migration | Grace 30 ⇒ submission at due+29min `is_late=False`, due+31min `True` — knob only, no code change | D1-B |
| D2-D-3 | Attachment presign: `POST /api/v1/assignments/upload-url/` `{filename, content_type, size_bytes}` → validate vs `allowed_file_types`/`max_file_size_mb` knobs (D2-E owns those defaults — coordinate names via WORKLOG; if E hasn't landed them yet, add them with defaults yourself, append-only) → `{url, key}`, key `{schema_name}/assignments/{uuid}/{filename}` via `infrastructure.storage.s3_client.presign_upload` | `apps/assignments/views.py`, `services.py` | Disallowed type → 422 `file_type_not_allowed`; oversize → 422 `file_too_large`; issued key prefix equals `connection.schema_name` (asserted in test) | D2-D-2 |
| D2-D-4 | Services: `publish_assignment(a, actor)` (emits `assignment_published`); `submit(assignment, student, text, attachment_keys)` — validates active cohort membership, computes `is_late` vs `due_at + grace`, `attempt_number = last + 1`, rejects `attempt_number > max_resubmits + 1` with 422 `resubmit_limit_exceeded`, rejects `closed` assignments; `grade_submission(submission, score, rubric_scores, feedback, actor)` — validates rubric criteria match `assignment.rubric` and `Σ max_points ≤ max_score`, emits `submission_graded` | `apps/assignments/services.py` | Every rejection path returns its named error code in the TD-18 envelope; third resubmit with limit 2 → 422; rubric score for an unknown criterion → 422 field error | D2-D-1/2 |
| D2-D-5 | Plagiarism stub: `check_submission(submission) -> PlagiarismResult(status="not_implemented", score=None)` typed dataclass — interface only, no HTTP, called from no request path | `apps/assignments/services.py` | Importable, typed, unit-tested | D2-D-1 |
| D2-D-6 | Viewsets: assignments CRUD (teacher write, scoped to own cohorts; students read **published** assignments of their cohorts via selectors); submissions create/list/retrieve (student submits own; teacher lists per assignment); grade + request-ai-feedback actions. Per-action `required_perms`; matrix rows (append-only): `STUDENT += {"assignments:submit"}`. | `apps/assignments/views.py`, `serializers.py`, `urls.py`, `selectors.py`, `core/permissions.py` | Student of cohort X cannot read drafts nor other cohorts' assignments and cannot submit to them (404 via queryset scoping, not a 403 leak); teacher sees only own cohorts' submissions; query-count test on both lists | D2-D-4 |
| D2-D-7 | Signals + Celery: `assignment_published(assignment)`, `assignment_due_soon(assignment)`, `submission_graded(submission)`, `ai_feedback_requested(submission)` in `apps/assignments/signals.py` (all emit-only; D3-C notifications + D4-A AI consume). Celery `send_due_soon_reminders`: fan-out per tenant; published assignments with `due_at` within 24h and `due_soon_sent_at IS NULL` → emit + stamp. Beat: hourly (append to `CELERY_BEAT_SCHEDULE`). `POST .../submissions/{id}/request-ai-feedback/` → emit signal, 202 `{status:"queued"}`. Tick TASKS §12. | `apps/assignments/signals.py`, `celery_tasks/assignment_tasks.py`, `config/settings/base.py`, `views.py` | Due-soon task ×2 runs → one emission per assignment (stamp idempotency); zero imports of sms/email/push/anthropic anywhere in apps/assignments | D2-D-4/6 |

**Models:**
- `Assignment`: `cohort: FK cohorts.Cohort PROTECT`, `created_by: FK users.User SET_NULL null`, `title: Char(200)`, `description: Text blank`, `due_at: DateTime`, `attachments: JSON default list` (S3 keys), `rubric: JSON default list` (`[{criterion: str, max_points: int}]`, serializer-validated), `max_score: Decimal(6,2) default 100`, `max_resubmits: PositiveSmallInt null` (null ⇒ knob value), `status: Char choices draft|published|closed default draft`, `published_at: DateTime null`, `due_soon_sent_at: DateTime null`, timestamps; indexes `(cohort, due_at)`, `status`.
- `Submission`: `assignment: FK Assignment CASCADE`, `student: FK students.StudentProfile PROTECT`, `text: Text blank`, `attachments: JSON default list`, `submitted_at: DateTime auto`, `is_late: Bool default False`, `attempt_number: PositiveSmallInt default 1`, `status: Char choices submitted|graded|returned default submitted`; `UniqueConstraint(assignment, student, attempt_number)`; index `(assignment, student)`; `CheckConstraint(attempt_number >= 1)`.
- `SubmissionGrade`: `submission: OneToOne Submission CASCADE`, `score: Decimal(6,2)`, `rubric_scores: JSON default list`, `feedback: Text blank`, `ai_feedback: Text blank` (written by D4-A), `graded_by: FK users.User SET_NULL null`, `graded_at: DateTime auto`; `CheckConstraint(score >= 0)`.

**Endpoints:**
- `CRUD /api/v1/assignments/` — `assignments:read`/`assignments:write` per action
- `POST /api/v1/assignments/{id}/publish/` — `assignments:write`
- `POST /api/v1/assignments/upload-url/` — `assignments:write` (teachers) or `assignments:submit` (students) — `{url, key}`
- `POST /api/v1/assignments/{id}/submissions/` — `assignments:submit` — 201 submission with `is_late`, `attempt_number`
- `GET /api/v1/assignments/{id}/submissions/` — `assignments:write` (teacher, own cohorts)
- `GET /api/v1/assignments/submissions/{id}/` — owner student or cohort teacher
- `POST /api/v1/assignments/submissions/{id}/grade/` `{score, rubric_scores, feedback}` — `assignments:write` — SubmissionGrade shape
- `POST /api/v1/assignments/submissions/{id}/request-ai-feedback/` — `assignments:write` — 202 `{status:"queued"}`

**Tests (`apps/assignments/tests/`):**
- `test_late_flag_boundaries_with_grace` (time-machine; due+grace exact = on time)
- `test_resubmit_limit_default_and_per_assignment_override`
- `test_rubric_validation_unknown_criterion_and_sum_cap`
- `test_draft_invisible_to_students` / `test_cross_cohort_submit_404`
- `test_due_soon_task_idempotent`
- `test_all_four_signals_emitted` (capture receivers)
- `test_upload_url_key_prefix_and_allowlist`
- cross-tenant per endpoint, query-count on assignments + submissions lists

**Publish to WORKLOG:** `ai_feedback_requested(submission)` signature + `SubmissionGrade.ai_feedback` field (D4-A writes it); `assignment_published`/`assignment_due_soon`/`submission_graded` signatures (D3-C); attachment-key convention `{schema_name}/assignments/...` (flag for D3-E quota metering scope).

---

## Lane E — Content + Storage (apps/content)

**Objective:** Replace `ContentItem` with the library hierarchy and the **canonical signed-URL storage flow**: presign → direct S3 PUT → confirm → async libmagic validation → final key → thumbnails. Visibility scoping, versioning, view/download tracking, tmp/ lifecycle, MinIO bootstrap, storage-quota interface for billing.
**Implements:** TASKS §13 + §23 (all except antivirus scan / video transcoding / AI summary — stub or deferred per spec; AI summary is D4-A), TD-13, TD-16 (`pillow`, `python-magic` / `python-magic-bin` on Windows). Production S3 is `[OWNER:O-9]` — MinIO/mock path is fully sufficient today per TD-2.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D2-E-1 | Models (below) + migration | `apps/content/models.py`, `migrations/`, `admin.py` | Fresh-DB migrate clean; `LessonFile` with neither `lesson` nor `folder` rejected by CheckConstraint | D2-C-1 merged (Subject) |
| D2-E-2 | Knobs: `allowed_file_types: JSON default ["pdf","mp4","pptx","docx","mp3","jpg","jpeg","png","webp"]`, `max_file_size_mb: int default 200`, `storage_quota_gb: int null` (null = unlimited) | per D1-B WORKLOG + migration | Upload-url issuance rejects per knob values; quota null ⇒ never blocks | D1-B |
| D2-E-3 | Upload flow: `request_upload(filename, content_type, size_bytes, user)` service → allowlist (extension **and** declared content-type) + size + quota checks → create `LessonFile(status=pending, s3_key="{schema_name}/tmp/{uuid}/{filename}")` → `presign_upload` URL; `POST /api/v1/content/files/{id}/confirm/` → 202, enqueues `validate_uploaded_file` | `apps/content/services.py`, `views.py`, `infrastructure/storage/s3_client.py` (add `head_object`, `copy_object`, `delete_object`, `get_object_range` helpers — additive) | 422 codes `file_type_not_allowed`, `file_too_large`, `storage_quota_exceeded`; confirm on a non-pending file → 409; **no S3 HTTP in the confirm handler** (presign generation is local signing — allowed in `upload-url`) | D2-E-1/2 |
| D2-E-4 | Celery `validate_uploaded_file(file_id)`: short-circuit unless `status=pending`; `head_object` size check; first 8KB via ranged GET → `magic.from_buffer` must match the declared type family, else `status=rejected` + `reject_reason`; on pass `copy_object` to `{schema_name}/content/{file_id}/{filename}`, delete tmp object, `status=clean`; enqueue `generate_thumbnail(file_id)` for image types (Pillow, 320px max edge, key `.../thumb.jpg`) | `celery_tasks/content_tasks.py` | PNG bytes declared as PDF → `rejected`; valid PDF → `clean` + final key; both tasks idempotent on re-run (status / existing-thumb short-circuit); retries ≤3 backoff | D2-E-3 |
| D2-E-5 | Download + tracking: `GET /api/v1/content/files/{id}/download-url/` → visibility check → signed GET TTL 300 → `F()`-increment `download_count` + `FileView(action=download)`; `POST .../files/{id}/track-view/` → `view_count` + `FileView(action=view)` | `apps/content/views.py`, `services.py`, `selectors.py` | Only `clean` files downloadable; counters use `F()` (race-safe, asserted); FileView rows created | D2-E-4 |
| D2-E-6 | Visibility scoping in selectors: `tenant` (everyone), `department` (RoleMembership.department match), `cohort` (member students / their parents / cohort teachers), `role` (`allowed_roles` JSON); versioning `POST .../files/{id}/new-version/` (re-runs upload flow, links `previous_version`, `version+1`) | `apps/content/selectors.py`, `views.py`, `services.py` | Student in cohort A cannot list or fetch a cohort-B file (404 via queryset, not 403); version chain walks back correctly | D2-E-3 |
| D2-E-7 | Hierarchy + Folder CRUD viewsets (`content:read`/`content:write` per action), filters, search on title, `@extend_schema` | `apps/content/views.py`, `serializers.py`, `urls.py` | All endpoints below live; library tree list constant-query (prefetch) | D2-E-1 |
| D2-E-8 | Dev storage bootstrap in `scripts/seed_dev.py` (idempotent): boto3 `create_bucket` if missing; lifecycle rule expiring the `tmp/` prefix after 7 days; CORS allowing PUT from `http://*.localhost:*` | `scripts/seed_dev.py` | Re-running seed is a no-op; fresh MinIO container → bucket + lifecycle + CORS present (assert via `get_bucket_lifecycle_configuration`) | — |
| D2-E-9 | Quota interface for billing: `apps.content.selectors.storage_used_bytes() -> int` (sum of `clean` LessonFile `size_bytes`) + enforcement in D2-E-3 vs `storage_quota_gb`. Tick TASKS §13 + §23 (built items only). | `apps/content/selectors.py` | Unit test: 2 files of known size sum correctly; an upload pushing the total over quota → 422 `storage_quota_exceeded` | D2-E-3 |

**Models:**
- `ContentLibrary`: `name: Char(200)`, `description: Text blank`, `visibility: Char choices tenant|department|cohort|role default tenant`, `department: FK org.Department SET_NULL null`, `cohort: FK cohorts.Cohort SET_NULL null`, `allowed_roles: JSON default list`, `is_active: Bool default True`, timestamps.
- `Course`: `library: FK ContentLibrary CASCADE`, `subject: FK academics.Subject PROTECT`, `title: Char(200)`, `description: Text blank`, `order: PositiveSmallInt default 0`.
- `Module`: `course: FK Course CASCADE`, `title: Char(200)`, `order: PositiveSmallInt default 0`; `UniqueConstraint(course, order)`.
- `ContentLesson` (named to avoid clashing with `schedule.Lesson`): `module: FK Module CASCADE`, `title: Char(200)`, `description: Text blank`, `order: PositiveSmallInt default 0`.
- `Folder`: `library: FK ContentLibrary CASCADE`, `parent: FK self CASCADE null`, `name: Char(200)`; `UniqueConstraint(library, parent, name)`.
- `LessonFile`: `lesson: FK ContentLesson CASCADE null`, `folder: FK Folder CASCADE null`, `title: Char(255)`, `s3_key: Char(512) unique`, `content_type: Char(127)`, `size_bytes: BigInt`, `status: Char choices pending|clean|rejected default pending`, `reject_reason: Char(255) blank`, `version: PositiveInt default 1`, `previous_version: FK self SET_NULL null`, `thumbnail_key: Char(512) blank`, `view_count: PositiveInt default 0`, `download_count: PositiveInt default 0`, `uploaded_by: FK users.User SET_NULL null`, timestamps; `CheckConstraint(lesson IS NOT NULL OR folder IS NOT NULL)`; indexes `status`, `(folder)`, `(lesson)`.
- `FileView`: `file: FK LessonFile CASCADE`, `user: FK users.User CASCADE`, `action: Char choices view|download`, `created_at`; index `(file, created_at)`.

**Endpoints:**
- `CRUD /api/v1/content/libraries|courses|modules|lessons|folders/` — `content:read`/`content:write` per action
- `POST /api/v1/content/upload-url/` — `content:write` — `{file_id, url, key, expires_in}`
- `POST /api/v1/content/files/{id}/confirm/` — `content:write` — 202 `{status:"pending"}`
- `GET /api/v1/content/files/` + `/{id}/` — `content:read` (visibility-scoped) — includes `status`, `version`, counters
- `GET /api/v1/content/files/{id}/download-url/` — `content:read` — `{url, expires_in}`
- `POST /api/v1/content/files/{id}/track-view/` — `content:read` — 204
- `POST /api/v1/content/files/{id}/new-version/` — `content:write` — same shape as upload-url

**Tests (`apps/content/tests/`):**
- `test_upload_url_allowlist_size_quota_rejections` (3 error codes)
- `test_magic_mismatch_rejected` / `test_valid_pdf_clean_and_moved`
- `test_validate_task_idempotent` / `test_thumbnail_idempotent`
- `test_only_clean_downloadable` / `test_counters_f_expression`
- `test_visibility_matrix_per_role` (tenant/department/cohort/role × roles)
- `test_version_chain`
- `test_every_issued_key_starts_with_schema_name`
- `test_seed_bootstrap_idempotent` (stub or live-MinIO marker)
- cross-tenant per endpoint, query-count on files + libraries lists

**Publish to WORKLOG:** the canonical upload-flow endpoints + `LessonFile` status machine (frontend contract, D5-D); `storage_used_bytes()` (D3-E billing meters it; D4-E control center displays it); new `s3_client` helper signatures; knob names (D2-D consumes `allowed_file_types`/`max_file_size_mb`).

---

## Lane F — Day-2 cross-cutting tests

**Objective:** Independent verification of every Day-2 surface: permission-matrix completeness, tenant isolation, query counts, conflict properties, storage flow, time-dependent windows. You merge **last**; expect to file fixes against other lanes' branches via WORKLOG, writing the failing test first.
**Implements:** TASKS §26 (matrix, object-scoping, isolation items for new resources), TD-16 addition, TD-20.

| ID | Task | Files to touch | Acceptance criteria | Depends on |
|---|---|---|---|---|
| D2-F-1 | Extend D1-E's parameterized permission-matrix test over Day-2 resources: every (role × {schedule, attendance, academics, assignments, content} × {read, write, submit}) ⇒ expected allow/deny from `ROLE_PERMISSION_MATRIX`; plus a fail-closed probe (viewset action with no `required_perms` entry → 403) | D1-E's matrix test module (path per D1-E WORKLOG) | One test, ~180 params, all green; deleting any Day-2 matrix row makes it fail | A–E merged |
| D2-F-2 | Cross-tenant isolation per new endpoint: with the D1-E two-tenant fixture, every Day-2 list/detail/action endpoint hit with a tenant-A token against tenant-B host → 401 `tenant_mismatch`; tenant-B rows never appear under tenant-A scoping | `tests/test_day2_isolation.py` (or D1-E's location) | Endpoint inventory derived from the DRF routers (no hand-kept list to rot); all pass | A–E |
| D2-F-3 | Query-count assertions on every Day-2 list endpoint (lessons, attendance records, dashboard, exams, grades, assignments, submissions, files, libraries) with `django_assert_num_queries` against seeded volume | same modules | Counts constant w.r.t. row count (run at 10 and 30 rows, identical count); any N+1 = red | A–E |
| D2-F-4 | Signed-URL flow integration: monkeypatch `infrastructure.storage.s3_client.get_s3_client` with an in-memory stub recording puts/copies/deletes (no new dep); plus one `@pytest.mark.minio` live test against the compose MinIO, auto-skipped when unreachable | `tests/storage_stub.py` (shared fixture), `apps/content/tests/` | Full pending→clean and pending→rejected paths pass on the stub; live test passes locally with `docker compose up minio` and skips cleanly in CI | E |
| D2-F-5 | Time-freeze tooling: add **`time-machine`** to dev deps (chosen over freezegun: C-level patching, faster, fewer side effects); use it for the correction-window (D2-B-3), late-flag (D2-D-4), and reminder/due-soon window tests; announce as a TD-16 addition in WORKLOG | `pyproject.toml` (append-only), B/D test modules | `uv run pytest -q` green; WORKLOG entry justifies the dep per TD-16 | B, D |
| D2-F-6 | Conflict-detection property tests: table-driven overlap cases (disjoint, touching edges, contained, spanning, identical, cross-midnight) × (room/teacher/cohort) × (API path **and** raw-ORM path — proving the exclusion constraint catches what services might miss) | `apps/schedule/tests/test_conflict_properties.py` | All cases pass at service level (409) and ORM level (IntegrityError); touching edges (end == start) is NOT a conflict | A |
| D2-F-7 | EOD sweep: run the full gate below; tick TASKS §26 items covered today; report coverage (floor 70% enforced, target ≥75% trending to TD-20's 80% after Day 3) | — | Gate checklist 100%; WORKLOG entry includes the coverage number | all |

**Publish to WORKLOG:** time-machine adoption (TD-16 list update — D3+ lanes use it, not freezegun); the storage-stub fixture path (D3-B payment tests + D4-B report tests reuse it); the router-derived endpoint-inventory helper (every later day's isolation tests reuse it).

---

## Cross-lane integration points (Day 2)

| Producer | Interface | Consumer |
|---|---|---|
| A | `schedule.Lesson`, `schedule.Term` models | B (`AttendanceRecord.lesson`), C (`Exam`/`Grade`/`Transcript.term`), F |
| C | `academics.Subject` | E (`Course.subject`) |
| D1-B / E | `CenterSettings` knobs; E owns `allowed_file_types`/`max_file_size_mb` defaults, D reads them | B, C, D — coordinate exact knob names via WORKLOG |
| A/B/C/D | Signals `lesson_reminder_due`, `lesson_cancelled`, `lesson_rescheduled`, `student_marked_absent`, `grade_changed`, `assignment_published`, `assignment_due_soon`, `submission_graded` | D3-C notifications, D3-D audit (emit-only today; capture-receiver tests pin the signatures) |
| D | `ai_feedback_requested(submission)` + `SubmissionGrade.ai_feedback` | D4-A AI |
| E | `storage_used_bytes()`, upload-flow contract, s3_client helpers | D3-E billing, D4-E control center, D5-D API contract |
| A/B/D | `CELERY_BEAT_SCHEDULE` entries (append-only in `config/settings/base.py`) | D4-F beat consolidation |

Merge order **A → B → C → D → E → F**. Migration collisions across different apps don't conflict; within `apps/org` (knob fields added by B/C/D/E) the **later merger** renumbers or runs `makemigrations --merge` (ROADMAP §2.3). Each lane appends only its own rows to `ROLE_PERMISSION_MATRIX` — never reorder others'.

---

## EOD gate (100% green before Day 2 closes)

- [ ] `uv run ruff check . && uv run ruff format --check .` — clean
- [ ] `uv run mypy apps core infrastructure config` — clean
- [ ] `uv run pytest -q` — all green on master after the final merge (F merges last)
- [ ] `uv run pytest --cov=apps --cov=core --cov-fail-under=70` passes; actual % logged in WORKLOG (target ≥75)
- [ ] Fresh-DB check: `migrate_schemas --shared` + provisioning a new Center runs every Day-2 migration (incl. btree_gist) without error
- [ ] OpenAPI: `/api/schema/` generates without warnings; every new endpoint has `@extend_schema` summary + tags; CI schema job green
- [ ] Demo script (against seeded `demo.localhost`, as director unless stated):
  1. Create Term + TimeSlot; create RecurrenceRule Mon/Wed 14:00 ×4 weeks → lessons materialized, holiday Monday skipped
  2. Create an overlapping rule for the same teacher → 409 `schedule_conflict` with conflicting lesson IDs
  3. Cancel one occurrence, move another (now detached), bulk-shift the rule +1 day — no conflicts
  4. Fetch `ical-url`, open the feed → valid calendar; the same token is rejected on a second tenant
  5. As teacher: mark attendance for today's lesson (one `late` via `arrived_at`, one `absent`) → `student_marked_absent` observed (log/test receiver); run `mark_absent_after_lesson` → unmarked students become `absent`; re-run = no-op
  6. Cohort dashboard + student term summary return correct rates; CSV export downloads
  7. Create Subject + 3 weighted Exams, enter results (one set via CSV), publish, recompute → Grade with the hand-checked weighted value; parent sees it only after publish
  8. Request transcript → 202; run worker → status `done`; signed URL downloads a real PDF
  9. Create Assignment with rubric + presigned attachment, publish; as student: submit late (frozen clock) → `is_late=True`; resubmit past the limit → 422; as teacher: grade with rubric; request AI feedback → 202 + `ai_feedback_requested` observed
  10. Content: upload-url → PUT to MinIO → confirm → worker validates (good PDF → `clean`, image gets a thumbnail; mislabeled file → `rejected`) → download-url works, counters increment; a cohort-scoped file is invisible to a non-member
- [ ] TASKS.md ticked: §9, §10, §11 (except AI exam-gen), §12, §13 + §23 (built items), §22 (`mark_absent_after_lesson`), §26 (Day-2 matrix/isolation items)
- [ ] WORKLOG entries appended by all six lanes, each listing the published interfaces above; time-machine TD-16 addition logged
- [ ] Layering spot-check: zero imports of `infrastructure.sms` / `infrastructure.email` / `infrastructure.ai` in `apps/schedule|attendance|academics|assignments|content`; no sync external HTTP in any request handler (docs/adding-an-app.md rules)
