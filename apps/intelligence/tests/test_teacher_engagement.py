"""A-3 teacher engagement facet — per-teacher attendance engagement + reach, gated
for dignity (a manager sees their branch's teachers; a teacher sees only their own).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TEACHERS = "/api/v1/intelligence/teachers/"


def _add_attendance(tenant, term, teacher, branch, profile):
    """Attach lessons + attendance marks to `teacher` per the profile (counts of
    present/late/excused/absent). No marks → the teacher appears with a null rate."""
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson
    from apps.students.tests.factories import StudentProfileFactory

    st = AttendanceRecord.Status
    with schema_context(tenant.schema_name):
        cohort = CohortFactory.create(branch=branch)
        student = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
        base = timezone.now() - timedelta(days=2)
        marks = (
            [st.PRESENT] * profile.get("present", 0)
            + [st.LATE] * profile.get("late", 0)
            + [st.EXCUSED] * profile.get("excused", 0)
            + [st.ABSENT] * profile.get("absent", 0)
        )
        for i, mark in enumerate(marks):
            lesson = Lesson.objects.create(
                term=term,
                cohort=cohort,
                teacher=teacher,
                title="L",
                starts_at=base + timedelta(hours=i * 2),
                ends_at=base + timedelta(hours=i * 2 + 1),
            )
            AttendanceRecord.objects.create(student=student, lesson=lesson, status=mark)


def _setup(tenant, user_in):
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
        term = TermFactory.create()
        # data-only teachers in A (low engagement) and B (other branch)
        t2 = TeacherProfileFactory.create(branch=branch_a)
        t3 = TeacherProfileFactory.create(branch=branch_b)
    # an authenticating teacher in A (needs the TEACHER role for intelligence:read)
    t1_u = user_in(tenant, roles=[Role.TEACHER], branch=branch_a)
    with schema_context(tenant.schema_name):
        t1 = TeacherProfileFactory.create(user=t1_u, branch=branch_a)
    # t1: 3 present + 1 late + 1 excused + 1 absent -> 4/5 non-excused = 80%
    _add_attendance(tenant, term, t1, branch_a, {"present": 3, "late": 1, "excused": 1, "absent": 1})
    _add_attendance(tenant, term, t2, branch_a, {"present": 1, "absent": 4})  # 20%
    _add_attendance(tenant, term, t3, branch_b, {"present": 5})  # 100%, other branch
    return {
        "branch_a": branch_a,
        "branch_b": branch_b,
        "t1": t1,
        "t1_u": t1_u,
        "t2": t2,
        "t3": t3,
    }


def test_engagement_rates_and_ordering(tenant_a, user_in, as_role):
    s = _setup(tenant_a, user_in)
    director, _ = as_role(Role.DIRECTOR)
    body = director.get(TEACHERS).json()["data"]
    rows = {r["teacher"]: r for r in body["results"]}
    assert rows[s["t1"].id]["attendance_rate"] == 80.0
    assert rows[s["t1"].id]["marks_sampled"] == 5  # the excused mark is excluded
    assert rows[s["t2"].id]["attendance_rate"] == 20.0
    assert rows[s["t3"].id]["attendance_rate"] == 100.0
    # best engagement first: t3 (100) before t1 (80) before t2 (20)
    order = [r["teacher"] for r in body["results"]]
    assert order.index(s["t3"].id) < order.index(s["t1"].id) < order.index(s["t2"].id)


def test_manager_sees_only_their_branch_teachers(tenant_a, user_in, as_role, as_user):
    s = _setup(tenant_a, user_in)
    hod_u = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=s["branch_a"])
    hod = as_user(tenant_a, hod_u)
    ids = {r["teacher"] for r in hod.get(TEACHERS).json()["data"]["results"]}
    assert s["t1"].id in ids
    assert s["t2"].id in ids
    assert s["t3"].id not in ids  # branch B is out of the HOD's scope


def test_teacher_sees_only_their_own_row(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in)
    me = as_user(tenant_a, s["t1_u"])
    rows = me.get(TEACHERS).json()["data"]["results"]
    # dignity: a teacher does not see a leaderboard of their peers
    assert [r["teacher"] for r in rows] == [s["t1"].id]
    assert rows[0]["attendance_rate"] == 80.0


def test_student_is_forbidden(tenant_a, user_in, as_role):
    _setup(tenant_a, user_in)
    student, _ = as_role(Role.STUDENT)  # students hold no intelligence:read
    assert student.get(TEACHERS).status_code == 403


def test_future_scheduled_lessons_are_not_counted_as_delivered(tenant_a, user_in, as_role):
    """A future lesson (materialized from a recurrence rule, status SCHEDULED) has not
    been delivered yet — it must not inflate lessons_delivered."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory

    s = _setup(tenant_a, user_in)  # t2 has 5 past lessons (1 present + 4 absent)
    with schema_context(tenant_a.schema_name):
        term = TermFactory.create()
        cohort = CohortFactory.create(branch=s["branch_a"])
        future = timezone.now() + timedelta(days=5)
        Lesson.objects.create(
            term=term,
            cohort=cohort,
            teacher=s["t2"],
            title="future",
            starts_at=future,
            ends_at=future + timedelta(hours=1),
        )
    director, _ = as_role(Role.DIRECTOR)
    rows = {r["teacher"]: r for r in director.get(TEACHERS).json()["data"]["results"]}
    assert rows[s["t2"].id]["lessons_delivered"] == 5  # the future lesson is excluded


def test_teacher_with_no_marks_has_null_rate_and_sorts_last(tenant_a, user_in, as_role):
    from apps.teachers.tests.factories import TeacherProfileFactory

    s = _setup(tenant_a, user_in)
    with schema_context(tenant_a.schema_name):
        idle = TeacherProfileFactory.create(branch=s["branch_a"])  # no lessons/attendance
    director, _ = as_role(Role.DIRECTOR)
    body = director.get(TEACHERS).json()["data"]
    idle_row = next(r for r in body["results"] if r["teacher"] == idle.id)
    assert idle_row["attendance_rate"] is None
    assert idle_row["engagement_score"] is None
    assert idle_row["lessons_delivered"] == 0
    assert body["results"][-1]["teacher"] == idle.id  # unscored sorts last
