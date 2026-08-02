"""A-3 — student dropout-risk flags (transparent rules over attendance / grades /
overdue payments), the risk feed, per-student detail, and the rules endpoint."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

RISK = "/api/v1/intelligence/risk/"
RULES = "/api/v1/intelligence/rules/"


def _attendance(student, statuses, *, days_ago=2):
    """Create `statuses` lessons (distinct times) + attendance records for a student.
    Call inside schema_context."""
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    cohort = student.current_cohort or CohortFactory.create(branch=student.branch)
    teacher = TeacherProfileFactory.create(branch=student.branch)
    term = TermFactory.create()
    base = timezone.now() - timedelta(days=days_ago)
    for i, status in enumerate(statuses):
        lesson = Lesson.objects.create(
            term=term,
            cohort=cohort,
            teacher=teacher,
            title="L",
            starts_at=base + timedelta(hours=i * 2),
            ends_at=base + timedelta(hours=i * 2 + 1),
        )
        AttendanceRecord.objects.create(student=student, lesson=lesson, status=status)


def test_low_grades_and_overdue_flags_via_api(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.finance.models import Invoice
    from apps.finance.tests.factories import InvoiceFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()
        exam = ExamFactory.create(is_published=True)
        ExamResultFactory.create(exam=exam, student=student, score=Decimal("30"))  # 30% < 50
        InvoiceFactory.create(student=student, status=Invoice.Status.OVERDUE)

    body = director.get(RISK).json()["data"]
    row = next(r for r in body["results"] if r["student"] == student.id)
    assert {f["code"] for f in row["flags"]} == {"low_grades", "overdue_payment"}
    assert row["score"] == 4
    assert row["level"] == "medium"


def test_transferred_student_does_not_carry_source_branch_debt_into_risk_feed(
    tenant_a,
    as_role,
):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.finance.models import Invoice
    from apps.finance.tests.factories import InvoiceFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        source = BranchFactory.create()
        destination = BranchFactory.create()
        source_cohort = CohortFactory.create(branch=source)
        destination_cohort = CohortFactory.create(branch=destination)
        student = StudentProfileFactory.create(branch=source, current_cohort=source_cohort)
        InvoiceFactory.create(student=student, status=Invoice.Status.OVERDUE)
        student.branch = destination
        student.current_cohort = destination_cohort
        student.save(update_fields=("branch", "current_cohort", "updated_at"))

    body = director.get(RISK).json()["data"]
    assert all(row["student"] != student.pk for row in body["results"])
    detail = director.get(f"{RISK}{student.pk}/").json()["data"]
    assert detail["flags"] == []


def test_healthy_student_is_not_flagged(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()
        exam = ExamFactory.create(is_published=True)
        ExamResultFactory.create(exam=exam, student=student, score=Decimal("90"))

    assert all(r["student"] != student.id for r in director.get(RISK).json()["data"]["results"])
    detail = director.get(f"{RISK}{student.id}/").json()["data"]
    assert detail["level"] == "none"
    assert detail["flags"] == []


def test_low_attendance_flag(tenant_a):
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.tests.factories import CohortFactory
    from apps.intelligence.selectors import student_risk
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        teacher = TeacherProfileFactory.create(branch=branch)
        cohort = CohortFactory.create(branch=branch)
        term = TermFactory.create()
        student = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
        base = timezone.now() - timedelta(days=2)
        for i in range(5):  # 3 absent of 5 = 60% >= 30%
            lesson = Lesson.objects.create(
                term=term,
                cohort=cohort,
                teacher=teacher,
                title="L",
                starts_at=base + timedelta(hours=i * 2),
                ends_at=base + timedelta(hours=i * 2 + 1),
            )
            status = AttendanceRecord.Status.ABSENT if i < 3 else AttendanceRecord.Status.PRESENT
            AttendanceRecord.objects.create(student=student, lesson=lesson, status=status)

        rows = student_risk(StudentProfile.objects.filter(pk=student.pk).select_related("user"))

    assert rows
    assert rows[0]["student"] == student.id
    assert any(f["code"] == "low_attendance" for f in rows[0]["flags"])
    assert rows[0]["level"] == "medium"  # weight 3


def test_risk_detail_404_for_unknown_student(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    assert director.get(f"{RISK}999999/").status_code == 404


def test_rules_endpoint_is_transparent(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    body = director.get(RULES).json()["data"]
    assert "low_attendance" in body["rules"]
    assert body["rules"]["low_attendance"]["weight"] == 3
    assert body["thresholds"]["low_grade_pct"] == 50.0


def test_intelligence_requires_permission(tenant_a, as_role):
    student_client, _ = as_role(Role.STUDENT)  # students hold no intelligence:read
    assert student_client.get(RISK).status_code == 403
    assert student_client.get(RULES).status_code == 403


# --------------------------------------------------------------------------- #
# review hardening
# --------------------------------------------------------------------------- #
def test_overdue_flag_hidden_from_non_finance_role(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.finance.models import Invoice
    from apps.finance.tests.factories import InvoiceFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    teacher_client, _t = as_role(Role.TEACHER)
    with schema_context(tenant_a.schema_name):
        from apps.cohorts.tests.factories import CohortFactory
        from apps.teachers.tests.factories import TeacherProfileFactory

        teacher_branch = _t.role_memberships.get(role=Role.TEACHER).branch
        teacher_profile = TeacherProfileFactory.create(user=_t, branch=teacher_branch)
        cohort = CohortFactory.create(branch=teacher_branch, primary_teacher=teacher_profile)
        student = StudentProfileFactory.create(branch=teacher_branch, current_cohort=cohort)
        exam = ExamFactory.create(is_published=True, cohort=cohort)
        ExamResultFactory.create(exam=exam, student=student, score=Decimal("30"))
        InvoiceFactory.create(student=student, status=Invoice.Status.OVERDUE)

    # the director (finance:read via *:*) sees the financial flag...
    drow = next(r for r in director.get(RISK).json()["data"]["results"] if r["student"] == student.id)
    assert {f["code"] for f in drow["flags"]} == {"low_grades", "overdue_payment"}
    # ...but a teacher (no finance:read) must NOT learn a student's arrears via the feed
    trow = next(r for r in teacher_client.get(RISK).json()["data"]["results"] if r["student"] == student.id)
    assert {f["code"] for f in trow["flags"]} == {"low_grades"}
    assert trow["score"] == 2  # the overdue weight is not added for a non-finance caller


def test_excused_excluded_and_late_not_counted_absent(tenant_a):
    from apps.attendance.models import AttendanceRecord
    from apps.intelligence.selectors import student_risk
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()
        # 2 absent + 1 present + 1 late (counted) + 3 excused (excluded from total).
        # total = 4, absent = 2 -> 50% >= 30% -> flagged. (Counting excused -> 28% -> not.)
        _attendance(
            student,
            [
                AttendanceRecord.Status.ABSENT,
                AttendanceRecord.Status.ABSENT,
                AttendanceRecord.Status.PRESENT,
                AttendanceRecord.Status.LATE,
                AttendanceRecord.Status.EXCUSED,
                AttendanceRecord.Status.EXCUSED,
                AttendanceRecord.Status.EXCUSED,
            ],
        )
        rows = student_risk(StudentProfile.objects.filter(pk=student.pk))
    assert rows
    flag = next(f for f in rows[0]["flags"] if f["code"] == "low_attendance")
    assert "of last 4 lessons" in flag["reason"]  # excused not in the denominator


def test_window_excludes_old_lessons(tenant_a):
    from apps.attendance.models import AttendanceRecord
    from apps.intelligence.selectors import student_risk
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()
        _attendance(
            student, [AttendanceRecord.Status.ABSENT] * 5, days_ago=100
        )  # all outside the 30-day window
        rows = student_risk(StudentProfile.objects.filter(pk=student.pk))
    assert all(r["student"] != student.id for r in rows)  # old lessons don't flag


def test_no_published_exams_not_flagged_on_grades(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()
        exam = ExamFactory.create(is_published=False)  # unpublished -> not counted
        ExamResultFactory.create(exam=exam, student=student, score=Decimal("5"))
    assert all(r["student"] != student.id for r in director.get(RISK).json()["data"]["results"])


def test_high_level_all_three_flags(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.attendance.models import AttendanceRecord
    from apps.finance.models import Invoice
    from apps.finance.tests.factories import InvoiceFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()
        exam = ExamFactory.create(is_published=True)
        ExamResultFactory.create(exam=exam, student=student, score=Decimal("20"))
        InvoiceFactory.create(student=student, status=Invoice.Status.OVERDUE)
        _attendance(student, [AttendanceRecord.Status.ABSENT] * 4)

    row = next(r for r in director.get(RISK).json()["data"]["results"] if r["student"] == student.id)
    assert {f["code"] for f in row["flags"]} == {"low_attendance", "low_grades", "overdue_payment"}
    assert row["score"] == 7
    assert row["level"] == "high"


def test_cohort_filter(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        c1 = CohortFactory.create(branch=branch)
        c2 = CohortFactory.create(branch=branch)
        s1 = StudentProfileFactory.create(branch=branch, current_cohort=c1)
        s2 = StudentProfileFactory.create(branch=branch, current_cohort=c2)
        for s in (s1, s2):
            exam = ExamFactory.create(is_published=True)
            ExamResultFactory.create(exam=exam, student=s, score=Decimal("10"))

    ids = {r["student"] for r in director.get(f"{RISK}?cohort={c1.id}").json()["data"]["results"]}
    assert s1.id in ids
    assert s2.id not in ids


def test_teacher_risk_scope_is_taught_cohorts_not_the_whole_branch(tenant_a, user_in, as_user):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    with schema_context(tenant_a.schema_name):
        teacher = TeacherProfileFactory.create(user=teacher_user, branch=branch)
        taught = CohortFactory.create(branch=branch, primary_teacher=teacher)
        untaught = CohortFactory.create(branch=branch)
        visible = StudentProfileFactory.create(branch=branch, current_cohort=taught)
        hidden = StudentProfileFactory.create(branch=branch, current_cohort=untaught)
        for cohort, student in ((taught, visible), (untaught, hidden)):
            exam = ExamFactory.create(is_published=True, cohort=cohort)
            ExamResultFactory.create(exam=exam, student=student, score=Decimal("10"))

    client = as_user(tenant_a, teacher_user)
    ids = {row["student"] for row in client.get(RISK).json()["data"]["results"]}
    assert visible.id in ids
    assert hidden.id not in ids
    assert client.get(f"{RISK}{hidden.id}/").status_code == 404


def test_future_attendance_does_not_inflate_current_risk(tenant_a):
    from apps.attendance.models import AttendanceRecord
    from apps.intelligence.selectors import student_risk
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()
        # The helper offsets from ``now - days_ago``; a negative value is in future.
        _attendance(student, [AttendanceRecord.Status.ABSENT] * 4, days_ago=-2)
        rows = student_risk(StudentProfile.objects.filter(pk=student.pk))
    assert all(row["student"] != student.id for row in rows)


def test_risk_pagination_invalid_filter_and_head(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        for _ in range(2):
            student = StudentProfileFactory.create()
            exam = ExamFactory.create(is_published=True)
            ExamResultFactory.create(exam=exam, student=student, score=Decimal("10"))

    body = director.get(f"{RISK}?page_size=1").json()["data"]
    assert body["count"] == 2
    assert len(body["results"]) == 1
    assert body["page"] == 1
    assert body["page_size"] == 1
    assert body["total_pages"] == 2
    invalid = director.get(f"{RISK}?cohort=not-an-integer")
    assert invalid.status_code == 400
    assert "cohort" in invalid.json()["errors"]
    for query, field in (
        ("cohort=0", "cohort"),
        ("cohort=1&cohort=2", "cohort"),
        ("page=0", "page"),
        ("page_size=101", "page_size"),
        ("brach=1", "brach"),
    ):
        rejected = director.get(f"{RISK}?{query}")
        assert rejected.status_code == 400
        assert field in rejected.json()["errors"]
    for url in (
        "/api/v1/intelligence/branches/?brach=1",
        "/api/v1/intelligence/families/?page=1&page=2",
        "/api/v1/intelligence/teachers/?page_size=101",
        f"{RISK}{student.id}/?unexpected=true",
        f"{RULES}?unexpected=true",
    ):
        assert director.get(url).status_code == 400
    assert director.head(RISK).status_code == 200
    assert director.head(f"{RISK}{student.id}/").status_code == 200
    assert director.head("/api/v1/intelligence/branches/").status_code == 200
    assert director.head("/api/v1/intelligence/families/").status_code == 200
    assert director.head("/api/v1/intelligence/teachers/").status_code == 200
    assert director.head(RULES).status_code == 200


def test_risk_page_query_count_is_population_invariant(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()
        exam = ExamFactory.create(is_published=True)
        ExamResultFactory.create(exam=exam, student=student, score=Decimal("10"))

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as small_capture:
        small = director.get(f"{RISK}?page_size=1")
    with schema_context(tenant_a.schema_name):
        for _ in range(40):
            student = StudentProfileFactory.create()
            exam = ExamFactory.create(is_published=True)
            ExamResultFactory.create(exam=exam, student=student, score=Decimal("10"))
    with CaptureQueriesContext(connection) as large_capture:
        large = director.get(f"{RISK}?page_size=1")

    assert small.status_code == 200, small.content
    assert large.status_code == 200, large.content
    assert small.json()["data"]["count"] == 1
    assert large.json()["data"]["count"] == 41
    assert len(small.json()["data"]["results"]) == 1
    assert len(large.json()["data"]["results"]) == 1
    assert len(large_capture) <= len(small_capture) + 1
