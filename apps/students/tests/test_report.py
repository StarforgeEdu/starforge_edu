"""F15-1 — the student-app report: per-lesson attendance sheet, bill paid-status, and
the student's own classroom rank (their position only, never a leaderboard)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

REPORT = "/api/v1/students/me/report/"


def test_student_report(tenant_a, user_in, as_user):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.tests.factories import CohortFactory
    from apps.finance.models import Invoice
    from apps.finance.tests.factories import InvoiceFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    St = AttendanceRecord.Status
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(branch=branch)
        me = StudentProfileFactory.create(user=student_user, branch=branch, current_cohort=cohort)
        # rank: me at 90% beats two classmates at 70% and 50% -> rank 1 of 3
        exam = ExamFactory.create(is_published=True, cohort=cohort)
        ExamResultFactory.create(exam=exam, student=me, score=Decimal("90"))
        for score in ("70", "50"):
            classmate = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
            ExamResultFactory.create(exam=exam, student=classmate, score=Decimal(score))
        # attendance: 2 present, 1 absent
        teacher = TeacherProfileFactory.create(branch=branch)
        term = TermFactory.create()
        base = timezone.now() - timedelta(days=2)
        for i, st in enumerate([St.PRESENT, St.PRESENT, St.ABSENT]):
            lesson = Lesson.objects.create(
                term=term,
                cohort=cohort,
                teacher=teacher,
                title="L",
                starts_at=base + timedelta(hours=i * 2),
                ends_at=base + timedelta(hours=i * 2 + 1),
            )
            AttendanceRecord.objects.create(student=me, lesson=lesson, status=st)
        InvoiceFactory.create(student=me, status=Invoice.Status.OVERDUE)

    body = as_user(tenant_a, student_user).get(REPORT).json()["data"]
    # attendance sheet (per-lesson) + summary
    assert len(body["attendance"]["sheet"]) == 3
    assert body["attendance"]["present"] == 2
    assert body["attendance"]["of"] == 3
    # own rank only — top of a 3-student class
    assert body["rank"]["rank"] == 1
    assert body["rank"]["of"] == 3
    # paid-status of the bills
    assert body["payment"]["has_overdue"] is True
    assert body["payment"]["latest_invoice"]["status"] == "overdue"


def test_ungraded_student_is_unranked(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(branch=branch)
        StudentProfileFactory.create(user=student_user, branch=branch, current_cohort=cohort)

    body = as_user(tenant_a, student_user).get(REPORT).json()["data"]
    assert body["rank"] is None  # no grades -> no rank, no division error
    assert body["attendance"]["rate"] is None  # no lessons yet


def test_attendance_rate_excludes_excused_and_counts_late(tenant_a, user_in, as_user):
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    St = AttendanceRecord.Status
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(branch=branch)
        me = StudentProfileFactory.create(user=student_user, branch=branch, current_cohort=cohort)
        teacher = TeacherProfileFactory.create(branch=branch)
        term = TermFactory.create()
        base = timezone.now() - timedelta(days=2)
        for i, st in enumerate([St.PRESENT, St.LATE, St.ABSENT, St.EXCUSED]):
            lesson = Lesson.objects.create(
                term=term,
                cohort=cohort,
                teacher=teacher,
                title="L",
                starts_at=base + timedelta(hours=i * 2),
                ends_at=base + timedelta(hours=i * 2 + 1),
            )
            AttendanceRecord.objects.create(student=me, lesson=lesson, status=st)

    att = as_user(tenant_a, student_user).get(REPORT).json()["data"]["attendance"]
    assert len(att["sheet"]) == 4  # the excused lesson still shows on the sheet
    assert att["of"] == 3  # ...but is dropped from the rate denominator
    assert att["present"] == 2  # present + late both count as attended
    assert att["rate"] == round(2 / 3, 3)


def test_rank_excludes_other_cohorts_unpublished_and_withdrawn(tenant_a, user_in, as_user):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(branch=branch)
        other_cohort = CohortFactory.create(branch=branch)
        published = ExamFactory.create(is_published=True, cohort=cohort)
        me = StudentProfileFactory.create(user=student_user, branch=branch, current_cohort=cohort)
        ExamResultFactory.create(exam=published, student=me, score=Decimal("70"))
        # an active classmate scoring higher -> I'm rank 2, not 1 (position is real)
        active = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
        ExamResultFactory.create(exam=published, student=active, score=Decimal("90"))
        # a WITHDRAWN classmate who outscored everyone -> must NOT count
        gone = StudentProfileFactory.create(
            branch=branch, current_cohort=cohort, status=StudentProfile.Status.WITHDRAWN
        )
        ExamResultFactory.create(exam=published, student=gone, score=Decimal("99"))
        # an UNPUBLISHED top mark for me -> must NOT lift my average
        draft = ExamFactory.create(is_published=False, cohort=cohort)
        ExamResultFactory.create(exam=draft, student=me, score=Decimal("100"))
        # a higher scorer in a DIFFERENT cohort -> must NOT count
        outsider = StudentProfileFactory.create(branch=branch, current_cohort=other_cohort)
        other_exam = ExamFactory.create(is_published=True, cohort=other_cohort)
        ExamResultFactory.create(exam=other_exam, student=outsider, score=Decimal("99"))

    rank = as_user(tenant_a, student_user).get(REPORT).json()["data"]["rank"]
    assert rank["rank"] == 2  # only me(70) + active(90) count -> I'm 2nd
    assert rank["of"] == 2
    assert set(rank.keys()) == {"rank", "of", "average_pct"}  # no classmate identity/scores leak


def test_report_404_for_non_student(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)  # has no student profile
    r = teacher.get(REPORT)
    assert r.status_code == 404
    assert r.json()["code"] == "not_a_student"
