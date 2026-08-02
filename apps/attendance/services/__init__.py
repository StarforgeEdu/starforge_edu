"""Attendance write services (TASKS §10, §22).

`mark_attendance` is the single teacher-facing write path (upsert, late
threshold, correction window); `auto_mark_absent` is the beat-task body that
back-fills `absent` no-shows. Both are emit-only — nothing here imports an
sms/email/push adapter (D3-C wires guardian dispatch off `student_marked_absent`).

These domain functions are preserved verbatim and re-exported from this package:
`auto_mark_absent` is imported by the celery beat task
(`celery_tasks/attendance_tasks.py`) and `mark_attendance` by the tests. The
layered service (services/v1/attendance_service.py) wraps `mark_attendance` after
resolving the entry student ids.
"""

from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.attendance.models import AttendanceRecord
from apps.attendance.signals import student_marked_absent, student_marked_late
from apps.cohorts.models import CohortMembership
from apps.org.selectors import get_center_settings
from apps.schedule.models import Lesson
from core.exceptions import PermissionException, UnprocessableEntity
from core.permissions import get_unambiguous_user_roles
from core.scoping import permission_membership_scopes
from core.utils import current_schema


def _attendance_write_scopes(actor):
    return permission_membership_scopes(
        roles=get_unambiguous_user_roles(actor),
        permission="attendance:write",
    )


def _permission_scope_allows_lesson(*, scopes, lesson: Lesson, account_kind: str) -> bool:
    """Whether one exact attendance-write membership covers ``lesson``.

    The aggregate permission union is intentionally insufficient: a teacher or
    manager grant in Branch A cannot be paired with a teaching assignment or an
    unrelated membership in Branch B.
    """
    department_id = lesson.cohort.department_id
    for membership in scopes:
        if membership.account_kind != account_kind:
            continue
        if membership.is_organization_wide:
            return True
        if membership.branch_id != lesson.cohort.branch_id:
            continue
        if membership.department_id is None or membership.department_id == department_id:
            return True
    return False


def _can_override_correction_window(scopes) -> bool:
    """Only an owner membership that itself grants attendance writes may override."""
    return any(
        membership.account_kind == "staff" and membership.is_organization_wide for membership in scopes
    )


def _assert_can_mark(lesson: Lesson, actor, *, write_scopes) -> None:
    """Actor must teach the lesson or manage its exact branch/department scope."""
    if actor.is_superuser or _can_override_correction_window(write_scopes):
        return
    if lesson.teacher.user_id == actor.id and _permission_scope_allows_lesson(
        scopes=write_scopes,
        lesson=lesson,
        account_kind="teacher",
    ):
        return
    if _permission_scope_allows_lesson(
        scopes=write_scopes,
        lesson=lesson,
        account_kind="staff",
    ):
        return
    raise PermissionException(
        _("Only the lesson's teacher can mark its attendance."), code="not_lesson_teacher"
    )


# F12/F15: a door card-scan checks a student in; the window around the scan time within
# which it matches (and marks present on) the lesson the student is arriving for.
SCAN_ATTENDANCE_WINDOW = dt.timedelta(minutes=30)


def mark_present_from_scan(*, student, at=None, marked_by=None) -> AttendanceRecord | None:
    """A card scan at the door marks the student PRESENT on their active-cohort lesson
    happening around ``at`` (±``SCAN_ATTENDANCE_WINDOW``). Purely ADDITIVE and safe for the
    money path (attendance feeds the A-1 absence-deduction):

    - it NEVER overrides an existing mark — a teacher's mark always wins;
    - it NEVER creates an absence — a scan can only record presence, never a penalty (and,
      by marking present, it also stops the auto-absent sweep from penalising a student who
      was actually here).

    Returns the created ``AttendanceRecord``, or ``None`` when there is no matching lesson
    or the lesson is already marked. Best-effort by contract — the caller (``scan_card``)
    must not let a failure here break the door check-in."""
    at = at or timezone.now()
    cohort_ids = list(
        CohortMembership.objects.filter(student=student, end_date__isnull=True).values_list(
            "cohort_id", flat=True
        )
    )
    if not cohort_ids:
        return None
    candidates = list(
        Lesson.objects.filter(
            cohort_id__in=cohort_ids,
            status=Lesson.Status.SCHEDULED,
            starts_at__lte=at + SCAN_ATTENDANCE_WINDOW,
            ends_at__gte=at - SCAN_ATTENDANCE_WINDOW,
        ).only("id", "starts_at", "ends_at")
    )
    if not candidates:
        return None

    # The lesson the student is ARRIVING for: prefer one currently IN SESSION, otherwise the
    # one whose start is nearest the scan time — NOT merely the earliest-starting overlapping
    # one, which (with the ±window) may already have ended, so back-to-back lessons don't get
    # the scan mis-attributed to the earlier class.
    def _arrival_key(lesson):
        in_session = lesson.starts_at <= at <= lesson.ends_at
        return (0 if in_session else 1, abs((lesson.starts_at - at).total_seconds()))

    lesson = min(candidates, key=_arrival_key)
    if AttendanceRecord.objects.filter(student=student, lesson=lesson).exists():
        return None  # never override an existing (teacher or prior) mark
    try:
        # Savepoint so a lost race (the partial unique on (student, lesson)) rolls back just
        # this insert, not the caller's whole transaction.
        with transaction.atomic():
            return AttendanceRecord.objects.create(
                student=student,
                lesson=lesson,
                status=AttendanceRecord.Status.PRESENT,
                arrived_at=at,
                marked_by=marked_by,
                auto_marked=True,
                note="card_scan",
            )
    except IntegrityError:
        return None  # a concurrent mark won the (student, lesson) unique race


def _assert_within_correction_window(lesson: Lesson, actor, settings, *, write_scopes) -> None:
    """Edits past `attendance_correction_window_hours` after the lesson ends are
    rejected — only a director may correct beyond the window (D2-B-3)."""
    if actor.is_superuser or _can_override_correction_window(write_scopes):
        return
    window = dt.timedelta(hours=settings.attendance_correction_window_hours)
    if timezone.now() > lesson.ends_at + window:
        raise PermissionException(
            _("The correction window for this lesson has closed."),
            code="correction_window_expired",
        )


def _resolve_status(entry: dict, lesson: Lesson, settings) -> str:
    """Resolve the stored status for one entry.

    The present-vs-late computation only applies when the teacher submitted
    `present` or `late`: a supplied `arrived_at` then decides present-vs-late —
    arriving more than `late_threshold_minutes` after the lesson start is `late`,
    exactly at the threshold is still `present` (boundary inclusive of on-time).
    An explicit `excused`/`absent` is preserved verbatim and never clobbered by
    `arrived_at`."""
    arrived_at = entry.get("arrived_at")
    submitted = entry["status"]
    if arrived_at is not None and submitted in (
        AttendanceRecord.Status.PRESENT,
        AttendanceRecord.Status.LATE,
    ):
        cutoff = lesson.starts_at + dt.timedelta(minutes=settings.late_threshold_minutes)
        return AttendanceRecord.Status.LATE if arrived_at > cutoff else AttendanceRecord.Status.PRESENT
    return submitted


def _emit_absent(record: AttendanceRecord, *, auto: bool, schema: str) -> None:
    transaction.on_commit(
        lambda: student_marked_absent.send(
            sender=AttendanceRecord,
            record_id=record.pk,
            student_id=record.student_id,
            lesson_id=record.lesson_id,
            auto=auto,
            schema_name=schema,
        )
    )


def _emit_late(record: AttendanceRecord, *, schema: str) -> None:
    transaction.on_commit(
        lambda: student_marked_late.send(
            sender=AttendanceRecord,
            record_id=record.pk,
            student_id=record.student_id,
            lesson_id=record.lesson_id,
            schema_name=schema,
        )
    )


@transaction.atomic
def mark_attendance(*, lesson: Lesson, entries: list[dict], actor) -> dict:
    """Upsert attendance for `lesson`. `entries` are dicts with `student` (a
    StudentProfile), `status`, optional `arrived_at`/`note`. Returns
    `{created, updated, records}`."""
    if lesson.status != Lesson.Status.SCHEDULED:
        raise UnprocessableEntity(
            _("Attendance can only be marked for a scheduled lesson."),
            code="lesson_not_scheduled",
        )
    if timezone.now() < lesson.starts_at:
        raise UnprocessableEntity(
            _("Attendance cannot be marked before the lesson starts."),
            code="lesson_not_started",
        )
    settings = get_center_settings()
    write_scopes = () if actor.is_superuser else _attendance_write_scopes(actor)
    _assert_can_mark(lesson, actor, write_scopes=write_scopes)
    _assert_within_correction_window(lesson, actor, settings, write_scopes=write_scopes)

    student_ids = [e["student"].pk for e in entries]
    # F2-6: membership is checked AS OF THE LESSON DATE, not "right now". A student
    # who was in the cohort when the lesson happened but has since been moved out
    # (move_student end-dates their membership) must still be markable — otherwise a
    # mid-session removal would block the teacher from recording the whole register.
    lesson_date = timezone.localdate(lesson.starts_at)
    member_ids = set(
        CohortMembership.objects.filter(
            cohort_id=lesson.cohort_id, student_id__in=student_ids, start_date__lte=lesson_date
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=lesson_date))
        .values_list("student_id", flat=True)
    )
    not_members = [sid for sid in student_ids if sid not in member_ids]
    if not_members:
        raise UnprocessableEntity(
            _("One or more students were not members of this lesson's cohort on the lesson date."),
            code="student_not_in_cohort",
            fields={"students": not_members},
        )

    # Snapshot prior statuses in ONE query so we can detect an absent -> present
    # CORRECTION below (update_or_create doesn't return the pre-image). A record that
    # leaves the absent set must retire any single-use absence-deduction credit it
    # spawned, or the student is credited for a lesson they actually attended.
    _ABSENT_LIKE = (AttendanceRecord.Status.ABSENT, AttendanceRecord.Status.EXCUSED)
    prior_status = dict(
        AttendanceRecord.objects.filter(lesson=lesson, student_id__in=student_ids).values_list(
            "student_id", "status"
        )
    )

    schema = current_schema()
    created = updated = 0
    records: list[AttendanceRecord] = []
    for entry in entries:
        status_value = _resolve_status(entry, lesson, settings)
        record, was_created = AttendanceRecord.objects.update_or_create(
            student=entry["student"],
            lesson=lesson,
            defaults={
                "status": status_value,
                "arrived_at": entry.get("arrived_at"),
                "note": entry.get("note", ""),
                "marked_by": actor,
                "auto_marked": False,
            },
        )
        created += int(was_created)
        updated += int(not was_created)
        records.append(record)
        if status_value == AttendanceRecord.Status.ABSENT:
            _emit_absent(record, auto=False, schema=schema)
        elif (
            status_value == AttendanceRecord.Status.LATE
            and prior_status.get(entry["student"].pk) != AttendanceRecord.Status.LATE
        ):
            _emit_late(record, schema=schema)

        if status_value not in _ABSENT_LIKE and prior_status.get(entry["student"].pk) in _ABSENT_LIKE:
            # Correction: this record was absent/excused and is now present/late. Retire
            # any approved single-use absence-deduction credit tied to it (no-op if none,
            # or if the credit was already applied to an invoice).
            from apps.approvals.services import clear_absence_deduction

            clear_absence_deduction(attendance_id=record.pk, actor=actor)
    return {"created": created, "updated": updated, "records": records}


_AUTO_ABSENCE_BATCH_SIZE = 100
_AUTO_ABSENCE_MAX_BATCHES_PER_RUN = 5


def auto_mark_absent() -> int:
    """Reconcile each due lesson exactly once, in atomically claimed batches.

    The prior sweep re-read every historical SCHEDULED lesson twice every 15 minutes,
    forever. ``auto_absence_processed_at`` is the durable idempotency marker and
    ``select_for_update(skip_locked=True)`` ensures concurrent/redelivered tenant tasks
    never process the same lesson together. Manual attendance still wins through the
    unique ``(student, lesson)`` record and ``get_or_create`` below.
    """
    settings = get_center_settings()
    cutoff = timezone.now() - dt.timedelta(minutes=settings.auto_absent_after_minutes)
    schema = current_schema()
    created = 0

    # Bound each tenant task even during the one-time post-migration backlog. Newest
    # eligible lessons are claimed first below so current attendance is never trapped
    # behind years of history; later beat runs drain the remaining marked backlog.
    for _batch_number in range(_AUTO_ABSENCE_MAX_BATCHES_PER_RUN):
        batch_created, processed = _process_auto_absence_batch(cutoff=cutoff, schema=schema)
        created += batch_created
        if processed < _AUTO_ABSENCE_BATCH_SIZE:
            return created
    return created


def _process_auto_absence_batch(*, cutoff, schema: str) -> tuple[int, int]:
    created = 0
    with transaction.atomic():
        lessons = list(
            Lesson.objects.select_for_update(skip_locked=True)
            .filter(
                status=Lesson.Status.SCHEDULED,
                starts_at__lte=cutoff,
                auto_absence_processed_at__isnull=True,
            )
            .only("id", "cohort_id", "starts_at", "auto_absence_processed_at")
            .order_by("-starts_at", "id")[:_AUTO_ABSENCE_BATCH_SIZE]
        )
        processed_at = timezone.now()
        for lesson in lessons:
            # F2-6 (symmetric with mark_attendance): roster as of the lesson date, so a
            # no-show moved out after the lesson still receives the historical absence.
            lesson_date = timezone.localdate(lesson.starts_at)
            member_ids = set(
                CohortMembership.objects.filter(
                    cohort_id=lesson.cohort_id,
                    start_date__lte=lesson_date,
                )
                .filter(Q(end_date__isnull=True) | Q(end_date__gte=lesson_date))
                .values_list("student_id", flat=True)
            )
            already = set(
                AttendanceRecord.objects.filter(lesson_id=lesson.pk).values_list("student_id", flat=True)
            )
            missing_ids = member_ids - already
            if missing_ids:
                # One INSERT for the whole roster instead of one get_or_create per
                # student. ignore_conflicts preserves a concurrent teacher/card mark;
                # the tagged follow-up query returns only rows this sweep inserted.
                AttendanceRecord.objects.bulk_create(
                    [
                        AttendanceRecord(
                            student_id=student_id,
                            lesson_id=lesson.pk,
                            status=AttendanceRecord.Status.ABSENT,
                            auto_marked=True,
                            marked_by=None,
                            note="auto_absent",
                        )
                        for student_id in missing_ids
                    ],
                    ignore_conflicts=True,
                )
                inserted = list(
                    AttendanceRecord.objects.filter(
                        lesson_id=lesson.pk,
                        student_id__in=missing_ids,
                        status=AttendanceRecord.Status.ABSENT,
                        auto_marked=True,
                        note="auto_absent",
                    )
                )
                created += len(inserted)
                for record in inserted:
                    _emit_absent(record, auto=True, schema=schema)
            lesson.auto_absence_processed_at = processed_at
        if lessons:
            Lesson.objects.bulk_update(lessons, ["auto_absence_processed_at"])
    return created, len(lessons)
