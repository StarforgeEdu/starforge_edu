"""Attendance lane tests (D2-B): upsert + late threshold + correction window,
auto-absent idempotency, the guardian-absence signal, summary/dashboard math,
CSV export, role scoping, cross-tenant isolation, and query budgets."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
import time_machine
from django.db import close_old_connections
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.attendance.models import AttendanceRecord
from apps.attendance.services import auto_mark_absent, mark_attendance
from apps.attendance.signals import student_marked_absent, student_marked_late
from apps.attendance.tests.factories import AttendanceRecordFactory
from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
from apps.org.models import CenterSettings
from apps.org.tests.factories import BranchFactory
from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
from apps.schedule.models import Lesson
from apps.schedule.tests.factories import TermFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.teachers.tests.factories import TeacherProfileFactory
from core.exceptions import PermissionException, UnprocessableEntity

pytestmark = pytest.mark.django_db

Status = AttendanceRecord.Status


def _aware(y, m, d, hh, mm=0):
    return timezone.make_aware(datetime(y, m, d, hh, mm))


def _make_lesson(*, branch, teacher, cohort=None, term=None, starts_at=None, ends_at=None) -> Lesson:
    cohort = cohort or CohortFactory(branch=branch)
    term = term or TermFactory()
    starts_at = starts_at or (timezone.now() - timedelta(hours=1))
    ends_at = ends_at or (starts_at + timedelta(hours=1))
    return Lesson.objects.create(
        term=term, cohort=cohort, teacher=teacher, title="Algebra", starts_at=starts_at, ends_at=ends_at
    )


def _enroll(cohort, *, n=1, branch=None) -> list[Any]:
    # factory-boy's typed stubs return the Factory class, not the instance, so the
    # produced rows are kept as Any (repo convention from the schedule lane).
    students: list[Any] = []
    for _ in range(n):
        student = StudentProfileFactory(branch=branch) if branch else StudentProfileFactory()
        CohortMembershipFactory(cohort=cohort, student=student)
        students.append(student)
    return students


def _grant_teacher_scope(user, branch) -> None:
    """Align legacy test actors with the branch their lesson belongs to."""
    from apps.users.models import RoleMembership
    from core.permissions import Role

    RoleMembership.objects.get_or_create(
        user=user,
        branch=branch,
        role=Role.TEACHER,
    )
    user.refresh_from_db()


# --------------------------------------------------------------------------- #
# mark_attendance — service-level behavior
# --------------------------------------------------------------------------- #


def test_mark_upserts_unique_per_student_lesson(tenant_a, user_in):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)
        (student,) = _enroll(lesson.cohort, branch=branch)

        first = mark_attendance(
            lesson=lesson, entries=[{"student": student, "status": Status.PRESENT}], actor=teacher_user
        )
        assert (first["created"], first["updated"]) == (1, 0)

        second = mark_attendance(
            lesson=lesson, entries=[{"student": student, "status": Status.ABSENT}], actor=teacher_user
        )
        assert (second["created"], second["updated"]) == (0, 1)
        records = AttendanceRecord.objects.filter(lesson=lesson, student=student)
        assert records.count() == 1
        assert records.get().status == Status.ABSENT


def test_teacher_of_other_cohort_denied(tenant_a, user_in):
    owner = user_in(tenant_a, roles=["teacher"])
    intruder = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(intruder, branch)
        profile = TeacherProfileFactory(user=owner, branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)
        (student,) = _enroll(lesson.cohort, branch=branch)
        with pytest.raises(PermissionException) as exc:
            mark_attendance(
                lesson=lesson, entries=[{"student": student, "status": Status.PRESENT}], actor=intruder
            )
        assert exc.value.code == "not_lesson_teacher"


def test_student_not_in_cohort_rejected(tenant_a, user_in):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)
        outsider: Any = StudentProfileFactory(branch=branch)  # never enrolled
        with pytest.raises(UnprocessableEntity) as exc:
            mark_attendance(
                lesson=lesson, entries=[{"student": outsider, "status": Status.PRESENT}], actor=teacher_user
            )
        assert exc.value.code == "student_not_in_cohort"
        assert outsider.pk in (exc.value.fields or {})["students"]


def test_future_lesson_cannot_be_pre_marked_service_or_api(tenant_a, user_in, as_user):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(
            branch=branch,
            teacher=profile,
            starts_at=timezone.now() + timedelta(hours=1),
        )
        (student,) = _enroll(lesson.cohort, branch=branch)

        with pytest.raises(UnprocessableEntity) as exc:
            mark_attendance(
                lesson=lesson,
                entries=[{"student": student, "status": Status.PRESENT}],
                actor=teacher_user,
            )
        assert exc.value.code == "lesson_not_started"

    response = as_user(tenant_a, teacher_user).post(
        f"/api/v1/attendance/lessons/{lesson.pk}/mark/",
        [{"student": student.pk, "status": Status.PRESENT}],
        format="json",
    )
    assert response.status_code == 422
    assert response.json()["code"] == "lesson_not_started"
    with schema_context(tenant_a.schema_name):
        assert not AttendanceRecord.objects.filter(lesson=lesson, student=student).exists()


@pytest.mark.parametrize(
    ("minutes_late", "expected"),
    [(10, Status.PRESENT), (11, Status.LATE)],  # threshold default 10: == present, +1 = late
)
def test_late_threshold_boundary(tenant_a, user_in, minutes_late, expected):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        starts_at = timezone.now() - timedelta(hours=1)
        lesson = _make_lesson(branch=branch, teacher=profile, starts_at=starts_at)
        (student,) = _enroll(lesson.cohort, branch=branch)
        result = mark_attendance(
            lesson=lesson,
            entries=[
                {
                    "student": student,
                    "status": Status.PRESENT,
                    "arrived_at": starts_at + timedelta(minutes=minutes_late),
                }
            ],
            actor=teacher_user,
        )
        assert result["records"][0].status == expected


def test_late_signal_emits_only_when_record_becomes_late(
    tenant_a, user_in, django_capture_on_commit_callbacks
):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    seen: list[dict] = []

    def capture(sender, **kwargs):
        seen.append(kwargs)

    student_marked_late.connect(capture, dispatch_uid="test.capture_late")
    try:
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            _grant_teacher_scope(teacher_user, branch)
            profile = TeacherProfileFactory(user=teacher_user, branch=branch)
            starts_at = timezone.now() - timedelta(hours=1)
            lesson = _make_lesson(branch=branch, teacher=profile, starts_at=starts_at)
            (student,) = _enroll(lesson.cohort, branch=branch)
            entry = {
                "student": student,
                "status": Status.PRESENT,
                "arrived_at": starts_at + timedelta(minutes=11),
            }

            with django_capture_on_commit_callbacks(execute=True):
                mark_attendance(lesson=lesson, entries=[entry], actor=teacher_user)
            with django_capture_on_commit_callbacks(execute=True):
                mark_attendance(lesson=lesson, entries=[entry], actor=teacher_user)
    finally:
        student_marked_late.disconnect(dispatch_uid="test.capture_late")

    assert len(seen) == 1
    assert seen[0]["student_id"] == student.pk
    assert seen[0]["lesson_id"] == lesson.pk


@pytest.mark.parametrize("submitted", [Status.EXCUSED, Status.ABSENT])
def test_arrived_at_never_clobbers_excused_or_absent(tenant_a, user_in, submitted):
    """An explicit excused/absent is stored verbatim even when `arrived_at` is
    within the present/late window — `arrived_at` only reshapes present-vs-late."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        starts_at = timezone.now() - timedelta(hours=1)
        lesson = _make_lesson(branch=branch, teacher=profile, starts_at=starts_at)
        (student,) = _enroll(lesson.cohort, branch=branch)
        result = mark_attendance(
            lesson=lesson,
            entries=[
                {
                    "student": student,
                    "status": submitted,
                    # On-time arrival (<= cutoff): would resolve to PRESENT if it
                    # leaked through, so any clobber would be visible here.
                    "arrived_at": starts_at + timedelta(minutes=5),
                }
            ],
            actor=teacher_user,
        )
        assert result["records"][0].status == submitted
        assert AttendanceRecord.objects.get(lesson=lesson, student=student).status == submitted


def test_late_threshold_knob_changes_behavior_no_code_change(tenant_a, user_in):
    """DoD #2 — bumping `late_threshold_minutes` shifts the boundary alone."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        from django.core.cache import cache

        settings = CenterSettings.load()
        settings.late_threshold_minutes = 20
        settings.save(update_fields=["late_threshold_minutes"])
        cache.clear()

        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        starts_at = timezone.now() - timedelta(hours=1)
        lesson = _make_lesson(branch=branch, teacher=profile, starts_at=starts_at)
        (student,) = _enroll(lesson.cohort, branch=branch)
        # 15 min late: `late` under the default 10, but `present` under 20.
        result = mark_attendance(
            lesson=lesson,
            entries=[
                {
                    "student": student,
                    "status": Status.PRESENT,
                    "arrived_at": starts_at + timedelta(minutes=15),
                }
            ],
            actor=teacher_user,
        )
        assert result["records"][0].status == Status.PRESENT


def test_correction_window_knob_changes_behavior_no_code_change(tenant_a, user_in):
    """DoD #2 — bumping `attendance_correction_window_hours` lets a teacher edit
    that was blocked at the default window succeed at the SAME frozen time."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        starts_at = _aware(2026, 6, 1, 10)
        lesson = _make_lesson(
            branch=branch, teacher=profile, starts_at=starts_at, ends_at=starts_at + timedelta(hours=1)
        )
        (student,) = _enroll(lesson.cohort, branch=branch)

    # 25h after the lesson ended — outside the default 24h window.
    with time_machine.travel(_aware(2026, 6, 2, 12)), schema_context(tenant_a.schema_name):
        with pytest.raises(PermissionException) as exc:
            mark_attendance(
                lesson=lesson,
                entries=[{"student": student, "status": Status.PRESENT}],
                actor=teacher_user,
            )
        assert exc.value.code == "correction_window_expired"

        # Bump the knob; the same edit at the same frozen time now succeeds.
        from django.core.cache import cache

        settings = CenterSettings.load()
        settings.attendance_correction_window_hours = 72
        settings.save(update_fields=["attendance_correction_window_hours"])
        cache.clear()

        result = mark_attendance(
            lesson=lesson,
            entries=[{"student": student, "status": Status.PRESENT}],
            actor=teacher_user,
        )
        assert (result["created"], result["updated"]) == (1, 0)
        assert AttendanceRecord.objects.get(lesson=lesson, student=student).status == Status.PRESENT


def test_auto_absent_knob_changes_behavior_no_code_change(tenant_a):
    """DoD #2 — lowering `auto_absent_after_minutes` sweeps a more-recent lesson
    that the default 30-min grace window would have skipped."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        # Started 20 min ago: inside the default 30-min grace → not swept.
        starts_at = timezone.now() - timedelta(minutes=20)
        lesson = _make_lesson(branch=branch, teacher=profile, starts_at=starts_at)
        _enroll(lesson.cohort, n=1, branch=branch)

        assert auto_mark_absent() == 0
        assert AttendanceRecord.objects.filter(lesson=lesson).count() == 0

        from django.core.cache import cache

        settings = CenterSettings.load()
        settings.auto_absent_after_minutes = 10  # now 20-min-old lesson is past cutoff
        settings.save(update_fields=["auto_absent_after_minutes"])
        cache.clear()

        assert auto_mark_absent() == 1
        assert AttendanceRecord.objects.get(lesson=lesson).status == Status.ABSENT


def test_correction_window_expired_teacher_403_director_ok(tenant_a, user_in, as_user):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    director_user = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        starts_at = _aware(2026, 6, 1, 10)
        lesson = _make_lesson(
            branch=branch, teacher=profile, starts_at=starts_at, ends_at=starts_at + timedelta(hours=1)
        )
        (student,) = _enroll(lesson.cohort, branch=branch)
        lesson_id, student_id, cohort_id = lesson.id, student.id, lesson.cohort_id

    body = [{"student": student_id, "status": "present"}]
    # Travel to 25h after the lesson ended (default correction window is 24h).
    # Tokens are minted INSIDE the travel so they aren't seen as expired.
    with time_machine.travel(_aware(2026, 6, 2, 12)):
        teacher_client = as_user(tenant_a, teacher_user)
        director_client = as_user(tenant_a, director_user)
        resp_t = teacher_client.post(f"/api/v1/attendance/lessons/{lesson_id}/mark/", body, format="json")
        assert resp_t.status_code == 403
        assert resp_t.json()["code"] == "correction_window_expired"

        resp_d = director_client.post(f"/api/v1/attendance/lessons/{lesson_id}/mark/", body, format="json")
        assert resp_d.status_code == 200
    with schema_context(tenant_a.schema_name):
        assert AttendanceRecord.objects.filter(lesson_id=lesson_id, student_id=student_id).exists()
        assert cohort_id  # silence unused


# --------------------------------------------------------------------------- #
# auto-absent sweep
# --------------------------------------------------------------------------- #


def test_auto_absent_idempotent_double_run(tenant_a, django_capture_on_commit_callbacks):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        starts_at = timezone.now() - timedelta(minutes=40)  # past auto_absent_after (30)
        lesson = _make_lesson(branch=branch, teacher=profile, starts_at=starts_at)
        _enroll(lesson.cohort, n=2, branch=branch)

        with django_capture_on_commit_callbacks(execute=True) as first:
            created_1 = auto_mark_absent()
        with django_capture_on_commit_callbacks(execute=True) as second:
            created_2 = auto_mark_absent()

        assert (created_1, len(first)) == (2, 2)  # 2 records, 2 signals
        assert (created_2, len(second)) == (0, 0)  # idempotent: nothing new
        assert AttendanceRecord.objects.filter(lesson=lesson).count() == 2
        assert AttendanceRecord.objects.filter(lesson=lesson, status=Status.ABSENT).count() == 2


def test_auto_absent_processed_marker_eliminates_historical_rescans(
    tenant_a,
    django_assert_max_num_queries,
):
    """Once reconciled, the beat cost is constant instead of two queries per old lesson."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        lesson_ids = []
        for days_ago in range(1, 21):
            lesson = _make_lesson(
                branch=branch,
                teacher=profile,
                starts_at=timezone.now() - timedelta(days=days_ago),
            )
            lesson_ids.append(lesson.pk)

        assert auto_mark_absent() == 0
        assert (
            Lesson.objects.filter(
                pk__in=lesson_ids,
                auto_absence_processed_at__isnull=False,
            ).count()
            == 20
        )

        # BEGIN + the indexed claim SELECT + COMMIT. The count does not grow with
        # the twenty (or twenty thousand) already-processed historical lessons.
        with django_assert_max_num_queries(3):
            assert auto_mark_absent() == 0


def test_auto_absent_run_is_bounded_during_a_large_backlog(tenant_a, monkeypatch):
    from apps.attendance import services

    calls = 0

    def _full_batch(*, cutoff, schema):
        nonlocal calls
        calls += 1
        return 0, services._AUTO_ABSENCE_BATCH_SIZE

    monkeypatch.setattr(services, "_process_auto_absence_batch", _full_batch)
    with schema_context(tenant_a.schema_name):
        assert services.auto_mark_absent() == 0
    assert calls == services._AUTO_ABSENCE_MAX_BATCHES_PER_RUN


def test_auto_absent_bulk_inserts_a_large_roster_in_constant_queries(
    tenant_a,
    django_assert_max_num_queries,
):
    from apps.org.selectors import get_center_settings

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        lesson = _make_lesson(
            branch=branch,
            teacher=profile,
            starts_at=timezone.now() - timedelta(minutes=40),
        )
        _enroll(lesson.cohort, n=30, branch=branch)
        get_center_settings()  # exclude the separately cached settings lookup

        # BEGIN, lesson claim, roster, existing marks, one bulk INSERT, inserted-row
        # fetch, marker update, COMMIT. This stays constant as the roster grows.
        with django_assert_max_num_queries(8):
            assert auto_mark_absent() == 30
        assert AttendanceRecord.objects.filter(lesson=lesson, note="auto_absent").count() == 30


@pytest.mark.django_db(transaction=True)
def test_concurrent_auto_absent_workers_claim_a_lesson_once(tenant_a, monkeypatch):
    from apps.org.selectors import get_center_settings

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        lesson = _make_lesson(
            branch=branch,
            teacher=profile,
            starts_at=timezone.now() - timedelta(minutes=40),
        )
        students = _enroll(lesson.cohort, n=12, branch=branch)
        lesson_id = lesson.pk
        cohort_id = lesson.cohort_id
        term_id = lesson.term_id
        teacher_id = profile.pk
        branch_id = branch.pk
        student_ids = [student.pk for student in students]
        user_ids = [profile.user_id, *(student.user_id for student in students)]
        get_center_settings()  # warm the tenant-scoped cache before worker threads

    # Keep this test focused on row claiming; notification fan-out has its own tests.
    monkeypatch.setattr("apps.attendance.services._emit_absent", lambda *args, **kwargs: None)

    def _run():
        close_old_connections()
        try:
            with schema_context(tenant_a.schema_name):
                return auto_mark_absent()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=15) for future in (pool.submit(_run), pool.submit(_run))]

    assert sum(results) == 12
    with schema_context(tenant_a.schema_name):
        lesson = Lesson.objects.get(pk=lesson_id)
        assert lesson.auto_absence_processed_at is not None
        assert AttendanceRecord.objects.filter(lesson_id=lesson_id).count() == 12

        # transaction=True commits tenant rows and tenant tables are not flushed by
        # pytest's public-schema teardown. Clean up this graph explicitly.
        from apps.cohorts.models import Cohort, CohortMembership
        from apps.org.models import Branch
        from apps.schedule.models import Term
        from apps.students.models import StudentProfile
        from apps.teachers.models import TeacherProfile
        from apps.users.models import User

        AttendanceRecord.objects.filter(lesson_id=lesson_id).delete()
        Lesson.objects.filter(pk=lesson_id).delete()
        CohortMembership.objects.filter(cohort_id=cohort_id).delete()
        StudentProfile.objects.filter(pk__in=student_ids).delete()
        Cohort.objects.filter(pk=cohort_id).delete()
        TeacherProfile.objects.filter(pk=teacher_id).delete()
        Term.objects.filter(pk=term_id).delete()
        User.objects.filter(pk__in=user_ids).delete()
        Branch.objects.filter(pk=branch_id).delete()


def test_auto_absent_skips_marked_students(tenant_a, user_in):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        starts_at = timezone.now() - timedelta(minutes=40)
        lesson = _make_lesson(branch=branch, teacher=profile, starts_at=starts_at)
        present_student, absent_student = _enroll(lesson.cohort, n=2, branch=branch)
        mark_attendance(
            lesson=lesson,
            entries=[{"student": present_student, "status": Status.PRESENT}],
            actor=teacher_user,
        )

        created = auto_mark_absent()
        assert created == 1  # only the unmarked student
        assert AttendanceRecord.objects.get(lesson=lesson, student=present_student).status == Status.PRESENT
        assert AttendanceRecord.objects.get(lesson=lesson, student=absent_student).status == Status.ABSENT


def test_auto_absent_skips_future_and_cancelled_lessons(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        # Started only 5 min ago — inside the 30-min grace window.
        recent = _make_lesson(branch=branch, teacher=profile, starts_at=timezone.now() - timedelta(minutes=5))
        _enroll(recent.cohort, n=1, branch=branch)
        # Old but cancelled.
        cancelled = _make_lesson(
            branch=branch, teacher=profile, starts_at=timezone.now() - timedelta(hours=2)
        )
        cancelled.status = Lesson.Status.CANCELLED
        cancelled.save(update_fields=["status"])
        _enroll(cancelled.cohort, n=1, branch=branch)

        assert auto_mark_absent() == 0
        assert AttendanceRecord.objects.count() == 0


def test_manual_attendance_rejects_cancelled_lesson(tenant_a, user_in):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)
        lesson.status = Lesson.Status.CANCELLED
        lesson.save(update_fields=["status"])
        (student,) = _enroll(lesson.cohort, branch=branch)

        with pytest.raises(UnprocessableEntity) as exc:
            mark_attendance(
                lesson=lesson,
                entries=[{"student": student, "status": Status.ABSENT}],
                actor=teacher_user,
            )
        assert exc.value.code == "lesson_not_scheduled"
        assert not AttendanceRecord.objects.filter(lesson=lesson).exists()


# --------------------------------------------------------------------------- #
# signal
# --------------------------------------------------------------------------- #


def test_absence_signal_emitted_manual_and_auto(tenant_a, user_in, django_capture_on_commit_callbacks):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    received: list[dict] = []

    def _recv(sender, **kwargs):
        received.append(kwargs)

    student_marked_absent.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            _grant_teacher_scope(teacher_user, branch)
            profile = TeacherProfileFactory(user=teacher_user, branch=branch)
            starts_at = timezone.now() - timedelta(minutes=40)
            lesson = _make_lesson(branch=branch, teacher=profile, starts_at=starts_at)
            manual_student, auto_student = _enroll(lesson.cohort, n=2, branch=branch)

            with django_capture_on_commit_callbacks(execute=True):
                mark_attendance(
                    lesson=lesson,
                    entries=[{"student": manual_student, "status": Status.ABSENT}],
                    actor=teacher_user,
                )
            assert [k["auto"] for k in received] == [False]

            with django_capture_on_commit_callbacks(execute=True):
                auto_mark_absent()
            # The manually-absent student already has a record; only auto_student fires.
            assert [k["auto"] for k in received] == [False, True]
            assert received[-1]["student_id"] == auto_student.id
    finally:
        student_marked_absent.disconnect(_recv)


# --------------------------------------------------------------------------- #
# summary + dashboard math / budget
# --------------------------------------------------------------------------- #


def test_summary_math(tenant_a):
    from apps.attendance import selectors

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        term: Any = TermFactory()
        cohort = CohortFactory(branch=branch)
        (student,) = _enroll(cohort, branch=branch)
        # 10 records, one per lesson (unique student+lesson): 6 present, 2 absent,
        # 1 late, 1 excused → percent_present = 60.0.
        plan = [Status.PRESENT] * 6 + [Status.ABSENT] * 2 + [Status.LATE, Status.EXCUSED]
        for i, st in enumerate(plan):
            day_start = _aware(2026, 3, 1 + i, 9)
            lesson = _make_lesson(
                branch=branch,
                teacher=profile,
                cohort=cohort,
                term=term,
                starts_at=day_start,
                ends_at=day_start + timedelta(hours=1),
            )
            AttendanceRecordFactory(student=student, lesson=lesson, status=st)

        summary = selectors.term_summary(
            base_qs=AttendanceRecord.objects.all(), student_id=student.id, term_id=term.id
        )
        assert summary == {
            "present": 6,
            "absent": 2,
            "late": 1,
            "excused": 1,
            "percent_present": 60.0,
        }


def test_dashboard_query_budget(tenant_a, user_in, as_user, django_assert_max_num_queries):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        cohort: Any = CohortFactory(branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile, cohort=cohort)
        students = _enroll(cohort, n=30, branch=branch)
        for student in students:
            AttendanceRecordFactory(student=student, lesson=lesson, status=Status.PRESENT)
        cohort_id = cohort.id

    client = as_user(tenant_a, director)
    # Includes tenant resolution, billing, the shared availability/timezone
    # settings read, session auth, and live permission resolution.
    with django_assert_max_num_queries(8):
        resp = client.get(f"/api/v1/attendance/cohorts/{cohort_id}/dashboard/")
    body = resp.json()["data"]
    assert resp.status_code == 200
    assert len(body["students"]) == 30
    assert body["rate"] == 100.0


def test_dashboard_bad_date_returns_400_not_500(tenant_a, user_in, as_user):
    """A malformed ?date_from surfaces as the TD-18 400 envelope, not an
    uncaught ORM-level 500."""
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort: Any = CohortFactory(branch=branch)
        cohort_id = cohort.id

    client = as_user(tenant_a, director)
    resp = client.get(f"/api/v1/attendance/cohorts/{cohort_id}/dashboard/?date_from=garbage")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_query_param"

    # A well-formed ISO datetime still works.
    ok = client.get(f"/api/v1/attendance/cohorts/{cohort_id}/dashboard/?date_from=2026-06-01T00:00:00Z")
    assert ok.status_code == 200


# --------------------------------------------------------------------------- #
# API surface — gating, scoping, export, cross-tenant, budget
# --------------------------------------------------------------------------- #


def test_mark_requires_write_perm(tenant_a, as_role):
    from core.permissions import Role

    client, _ = as_role(Role.STUDENT)  # student has attendance:read, not :write
    resp = client.post("/api/v1/attendance/lessons/1/mark/", [], format="json")
    assert resp.status_code == 403


def test_records_list_scoping_student_parent_teacher(tenant_a, user_in, as_user):
    student_user = user_in(tenant_a, roles=["student"])
    parent_user = user_in(tenant_a, roles=["parent"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()

    # The permission-bearing teacher membership and the teaching assignment are
    # intentionally in the same branch. Cross-branch assignment denial has its
    # own regression below and must not be smuggled into this valid-scope fixture.
    teacher_user = user_in(tenant_a, roles=["teacher"], branch=branch)
    with schema_context(tenant_a.schema_name):
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)

        my_student: Any = StudentProfileFactory(user=student_user, branch=branch)
        CohortMembershipFactory(cohort=lesson.cohort, student=my_student)
        (other_student,) = _enroll(lesson.cohort, branch=branch)

        # A record for each student, both on the teacher's lesson.
        AttendanceRecordFactory(student=my_student, lesson=lesson, status=Status.PRESENT)
        AttendanceRecordFactory(student=other_student, lesson=lesson, status=Status.ABSENT)

        # Parent links to my_student.
        parent_profile = ParentProfileFactory(user=parent_user)
        GuardianFactory(parent=parent_profile, student=my_student)

        # A record on a DIFFERENT teacher's lesson — the teacher must not see it.
        other_profile = TeacherProfileFactory(branch=branch)
        foreign_lesson = _make_lesson(branch=branch, teacher=other_profile)
        foreign_student = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=foreign_lesson.cohort, student=foreign_student)
        AttendanceRecordFactory(student=foreign_student, lesson=foreign_lesson, status=Status.ABSENT)

        my_student_id, foreign_lesson_id = my_student.id, foreign_lesson.id

    # Student sees only their own record.
    student_body = as_user(tenant_a, student_user).get("/api/v1/attendance/records/").json()
    assert {r["student"] for r in student_body["data"]} == {my_student_id}

    # Parent sees only the linked child's record.
    parent_body = as_user(tenant_a, parent_user).get("/api/v1/attendance/records/").json()
    assert {r["student"] for r in parent_body["data"]} == {my_student_id}

    # Teacher sees only records on lessons they teach (2 records), not the foreign one.
    teacher_body = as_user(tenant_a, teacher_user).get("/api/v1/attendance/records/").json()
    assert teacher_body["pagination"]["total"] == 2
    assert all(r["lesson"] != foreign_lesson_id for r in teacher_body["data"])


def test_teacher_assignment_cannot_borrow_attendance_scope_from_another_branch(tenant_a, user_in, as_user):
    """A malformed cross-branch teaching assignment is not an authorization grant.

    Natural ownership and the exact permission-bearing teacher membership must
    intersect for both record reads and the whole-class dashboard.
    """
    from apps.users.models import RoleMembership
    from core.permissions import Role

    teacher_user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        cohort_branch = BranchFactory()
        membership_branch = BranchFactory()
        RoleMembership.objects.create(
            user=teacher_user,
            branch=membership_branch,
            role=Role.TEACHER,
        )
        profile = TeacherProfileFactory(user=teacher_user, branch=membership_branch)
        cohort = CohortFactory(branch=cohort_branch, primary_teacher=profile)
        lesson = _make_lesson(branch=cohort_branch, teacher=profile, cohort=cohort)
        student = StudentProfileFactory(branch=cohort_branch, current_cohort=cohort)
        CohortMembershipFactory(cohort=cohort, student=student)
        record = AttendanceRecordFactory(student=student, lesson=lesson, status=Status.PRESENT)
        teacher_user.refresh_from_db()

    client = as_user(tenant_a, teacher_user)
    listing = client.get("/api/v1/attendance/records/")
    assert listing.status_code == 200
    assert record.id not in {row["id"] for row in listing.json()["data"]}

    dashboard = client.get(f"/api/v1/attendance/cohorts/{cohort.id}/dashboard/")
    assert dashboard.status_code == 403
    assert dashboard.json()["code"] == "out_of_scope"


def test_hod_attendance_is_department_scoped_for_reads_dashboard_and_mark(tenant_a, user_in, as_user):
    """A department HoD cannot turn attendance list or action ids into a tenant-wide IDOR."""
    from apps.org.tests.factories import DepartmentFactory
    from apps.users.models import RoleMembership
    from core.permissions import Role

    hod = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        other_branch = BranchFactory()
        own_department = DepartmentFactory(branch=branch)
        sibling_department = DepartmentFactory(branch=branch)
        foreign_department = DepartmentFactory(branch=other_branch)
        RoleMembership.objects.create(
            user=hod,
            branch=branch,
            department=own_department,
            role=Role.HEAD_OF_DEPT,
        )
        hod.refresh_from_db()

        def _record_for(department, record_branch):
            cohort = CohortFactory(branch=record_branch, department=department)
            teacher = TeacherProfileFactory(branch=record_branch, department=department)
            lesson = _make_lesson(branch=record_branch, teacher=teacher, cohort=cohort)
            student = StudentProfileFactory(
                branch=record_branch,
                current_cohort=cohort,
            )
            CohortMembershipFactory(cohort=cohort, student=student)
            record = AttendanceRecordFactory(
                student=student,
                lesson=lesson,
                status=Status.PRESENT,
            )
            return cohort, lesson, student, record

        own = _record_for(own_department, branch)
        sibling = _record_for(sibling_department, branch)
        foreign = _record_for(foreign_department, other_branch)

    client = as_user(tenant_a, hod)
    listing = client.get("/api/v1/attendance/records/")
    assert listing.status_code == 200
    assert {row["id"] for row in listing.json()["data"]} == {own[3].id}
    assert client.get(f"/api/v1/attendance/records/{sibling[3].id}/").status_code == 404
    assert client.get(f"/api/v1/attendance/records/{foreign[3].id}/").status_code == 404

    assert client.get(f"/api/v1/attendance/cohorts/{own[0].id}/dashboard/").status_code == 200
    denied_dashboard = client.get(f"/api/v1/attendance/cohorts/{sibling[0].id}/dashboard/")
    assert denied_dashboard.status_code == 403
    assert denied_dashboard.json()["code"] == "out_of_scope"

    denied_mark = client.post(
        f"/api/v1/attendance/lessons/{sibling[1].id}/mark/",
        [{"student": sibling[2].id, "status": Status.ABSENT}],
        format="json",
    )
    assert denied_mark.status_code == 404
    assert denied_mark.json()["code"] == "not_found"

    allowed_mark = client.post(
        f"/api/v1/attendance/lessons/{own[1].id}/mark/",
        [{"student": own[2].id, "status": Status.ABSENT}],
        format="json",
    )
    assert allowed_mark.status_code == 200, allowed_mark.content


def test_record_payload_surfaces_group_and_teacher(tenant_a, user_in, as_user):
    """The attendance record answers 'which group / which teacher' (the owner's
    screenshot gap) directly from the lesson, with no extra query per row."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()

    teacher_user = user_in(tenant_a, roles=["teacher"], branch=branch)
    with schema_context(tenant_a.schema_name):
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)
        (student,) = _enroll(lesson.cohort, branch=branch)
        AttendanceRecordFactory(student=student, lesson=lesson, status=Status.PRESENT)
        cohort_id, cohort_name, teacher_id = lesson.cohort_id, lesson.cohort.name, profile.id

    body = as_user(tenant_a, teacher_user).get("/api/v1/attendance/records/").json()
    rec = body["data"][0]
    assert rec["cohort"] == cohort_id
    assert rec["cohort_name"] == cohort_name
    assert rec["teacher"] == teacher_id
    assert rec["teacher_name"] == teacher_user.get_full_name()
    assert "lesson_starts_at" in rec


def test_csv_export_shape(tenant_a, user_in, as_user):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        cohort = CohortFactory(branch=branch)
        students = _enroll(cohort, n=3, branch=branch)
        for i, student in enumerate(students):
            day = _aware(2026, 4, 1 + i, 9)
            lesson = _make_lesson(
                branch=branch, teacher=profile, cohort=cohort, starts_at=day, ends_at=day + timedelta(hours=1)
            )
            AttendanceRecordFactory(student=student, lesson=lesson, status=Status.PRESENT)
        record_count = AttendanceRecord.objects.count()

    resp = as_user(tenant_a, director).get("/api/v1/attendance/export/")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/csv"
    content = b"".join(resp.streaming_content).decode()
    rows = [line for line in content.splitlines() if line]
    assert rows[0] == "date,lesson,student,status,marked_by"
    assert len(rows) == record_count + 1  # header + one row per record


def test_csv_export_neutralizes_formula_cells_and_has_constant_query_count(
    tenant_a,
    user_in,
    as_user,
    django_assert_max_num_queries,
):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        cohort = CohortFactory(branch=branch)
        students = _enroll(cohort, n=8, branch=branch)
        for index, student in enumerate(students):
            student.first_name = "=HYPERLINK"
            student.last_name = f"row-{index}"
            student.save(update_fields=("first_name", "last_name"))
            starts_at = _aware(2026, 5, 1, 9) + timedelta(days=index)
            lesson = _make_lesson(
                branch=branch,
                teacher=profile,
                cohort=cohort,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
            )
            lesson.title = "@SUM(A1:A2)"
            lesson.save(update_fields=("title",))
            AttendanceRecordFactory(student=student, lesson=lesson, status=Status.PRESENT)

    client = as_user(tenant_a, director)
    with django_assert_max_num_queries(9):
        response = client.get("/api/v1/attendance/export/")
        content = b"".join(response.streaming_content).decode()

    assert response.status_code == 200
    assert "'@SUM(A1:A2)" in content
    assert "'=HYPERLINK" in content


def test_csv_export_refuses_unbounded_result(monkeypatch, tenant_a, user_in, as_user):
    director = user_in(tenant_a, roles=["director"])
    monkeypatch.setattr("apps.attendance.views.v1.attendance_views.MAX_EXPORT_ROWS", -1)

    response = as_user(tenant_a, director).get("/api/v1/attendance/export/")

    assert response.status_code == 400
    assert response.json()["code"] == "export_too_large"


def test_records_list_cross_tenant_isolated(tenant_a, tenant_b, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)
        (student,) = _enroll(lesson.cohort, branch=branch)
        AttendanceRecordFactory(student=student, lesson=lesson, status=Status.PRESENT)

    director_b = user_in(tenant_b, roles=["director"])
    body = as_user(tenant_b, director_b).get("/api/v1/attendance/records/").json()
    assert body["pagination"]["total"] == 0  # tenant_a's record is invisible from tenant_b


def test_mark_cross_tenant_lesson_404(tenant_a, tenant_b, user_in, as_user):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)
        lesson_id = lesson.id

    # A tenant_b teacher cannot reach tenant_a's lesson id — it does not exist there.
    teacher_b = user_in(tenant_b, roles=["teacher"])
    resp = as_user(tenant_b, teacher_b).post(
        f"/api/v1/attendance/lessons/{lesson_id}/mark/", [], format="json"
    )
    assert resp.status_code == 404


def test_records_list_query_budget(tenant_a, user_in, as_user, django_assert_max_num_queries):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(branch=branch)
        cohort = CohortFactory(branch=branch)
        students = _enroll(cohort, n=5, branch=branch)
        for i, student in enumerate(students):
            day = _aware(2026, 5, 1 + i, 9)
            lesson = _make_lesson(
                branch=branch, teacher=profile, cohort=cohort, starts_at=day, ends_at=day + timedelta(hours=1)
            )
            AttendanceRecordFactory(student=student, lesson=lesson, status=Status.PRESENT)

    client = as_user(tenant_a, director)
    with django_assert_max_num_queries(9):  # +1: A-2 per-request permission-override load
        body = client.get("/api/v1/attendance/records/").json()
    assert set(body) == {"success", "data", "pagination"}
    assert body["pagination"]["total"] == 5


# --------------------------------------------------------------------------- #
# F2-6 — attendance tolerates a mid-session membership change
# --------------------------------------------------------------------------- #


def test_attendance_tolerates_a_student_moved_after_the_lesson(tenant_a, user_in):
    """A student moved out of the cohort AFTER attending the lesson must still be
    markable — membership is checked as of the lesson date, not 'right now'."""
    from apps.cohorts.services import move_student

    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)  # cohort A, started ~1h ago
        (student,) = _enroll(lesson.cohort, branch=branch)
        other = CohortFactory(branch=branch)
        move_student(student=student, to_cohort=other)  # end-dates the cohort-A membership today
        result = mark_attendance(
            lesson=lesson, entries=[{"student": student, "status": Status.PRESENT}], actor=teacher_user
        )
        assert result["created"] == 1


def test_attendance_rejects_a_student_who_left_before_the_lesson(tenant_a, user_in):
    """The as-of-date check still rejects a student whose membership ended BEFORE the
    lesson (they genuinely weren't in the cohort when it happened)."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(branch=branch, teacher=profile)
        student: Any = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(
            cohort=lesson.cohort,
            student=student,
            end_date=timezone.localdate(lesson.starts_at) - timedelta(days=1),  # left the day before
        )
        with pytest.raises(UnprocessableEntity) as exc:
            mark_attendance(
                lesson=lesson, entries=[{"student": student, "status": Status.PRESENT}], actor=teacher_user
            )
        assert exc.value.code == "student_not_in_cohort"


def test_auto_mark_absent_includes_a_student_moved_after_the_lesson(tenant_a):
    """The absent-sweep is symmetric with mark_attendance: a no-show moved out after
    the lesson still gets their absent record for the lesson they missed."""
    from apps.cohorts.services import move_student

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher = TeacherProfileFactory(branch=branch)
        lesson = _make_lesson(branch=branch, teacher=teacher, starts_at=timezone.now() - timedelta(hours=2))
        (student,) = _enroll(lesson.cohort, branch=branch)  # never marked -> a no-show
        move_student(student=student, to_cohort=CohortFactory(branch=branch))
        assert auto_mark_absent() == 1
        record = AttendanceRecord.objects.get(lesson=lesson, student=student)
        assert record.status == Status.ABSENT
        assert record.auto_marked is True


@time_machine.travel("2026-06-10 20:30:00 +00:00", tick=False)
def test_attendance_membership_uses_center_local_lesson_date(tenant_a, user_in):
    """20:30 UTC is already the next calendar day in Asia/Tashkent. Membership
    dates and lesson-date checks must use that same center-local day."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _grant_teacher_scope(teacher_user, branch)
        teacher = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _make_lesson(
            branch=branch,
            teacher=teacher,
            starts_at=timezone.now() - timedelta(hours=1),
        )
        student: Any = StudentProfileFactory(branch=branch, current_cohort=lesson.cohort)
        CohortMembershipFactory(
            cohort=lesson.cohort,
            student=student,
            start_date=timezone.localdate(lesson.starts_at),
        )

        result = mark_attendance(
            lesson=lesson,
            entries=[{"student": student, "status": Status.PRESENT}],
            actor=teacher_user,
        )

        assert result["created"] == 1
