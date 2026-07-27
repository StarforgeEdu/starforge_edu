"""A-3 facet — branch performance ranking: a transparent 0-100 score over each
branch's attendance, published grades, and dropout-risk, highest first. Branches too
small to anonymise are suppressed; branches with no academic signal stay unranked."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

BRANCHES = "/api/v1/intelligence/branches/"


def _make_branch(tenant, profiles):
    """A branch populated with one active student per entry in `profiles`. Each student
    gets its OWN cohort + teacher so the schedule's no-overlap constraints never fire.
    profile keys: present/late/excused/absent (mark counts), grade (score or None),
    overdue (bool)."""
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

    St = AttendanceRecord.Status
    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        term = TermFactory.create()
        base = timezone.now() - timedelta(days=2)
        for p in profiles:
            cohort = CohortFactory.create(branch=branch)
            teacher = TeacherProfileFactory.create(branch=branch)
            student = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
            marks = (
                [St.PRESENT] * p.get("present", 0)
                + [St.LATE] * p.get("late", 0)
                + [St.EXCUSED] * p.get("excused", 0)
                + [St.ABSENT] * p.get("absent", 0)
            )
            for i, st in enumerate(marks):
                lesson = Lesson.objects.create(
                    term=term,
                    cohort=cohort,
                    teacher=teacher,
                    title="L",
                    starts_at=base + timedelta(hours=i * 2),
                    ends_at=base + timedelta(hours=i * 2 + 1),
                )
                AttendanceRecord.objects.create(student=student, lesson=lesson, status=st)
            if p.get("grade") is not None:
                exam = ExamFactory.create(is_published=True, cohort=cohort)
                ExamResultFactory.create(exam=exam, student=student, score=Decimal(str(p["grade"])))
            if p.get("overdue"):
                InvoiceFactory.create(student=student, status=Invoice.Status.OVERDUE)
    return branch


def _row(client, branch_id):
    return next(r for r in client.get(BRANCHES).json()["data"]["results"] if r["branch"] == branch_id)


def test_branch_ranking_orders_by_performance(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    good = _make_branch(tenant_a, [{"present": 5, "grade": 90}] * 3)
    poor = _make_branch(tenant_a, [{"present": 1, "absent": 4, "grade": 30, "overdue": True}] * 3)

    rows = {r["branch"]: r for r in director.get(BRANCHES).json()["data"]["results"]}
    # good: 1.0*50 + 0.9*30 + (1-0)*20 = 97.0   poor: 0.2*50 + 0.3*30 + (1-1)*20 = 19.0
    assert rows[good.id]["score"] == 97.0
    assert rows[poor.id]["score"] == 19.0
    assert rows[good.id]["rank"] < rows[poor.id]["rank"]
    assert rows[good.id]["attendance_rate"] == 1.0
    assert rows[poor.id]["attendance_rate"] == 0.2
    assert rows[good.id]["at_risk"] == 0
    assert rows[poor.id]["at_risk"] == 3
    assert rows[poor.id]["overdue_students"] == 3
    assert rows[good.id]["suppressed"] is False


def test_small_branch_is_suppressed(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    tiny = _make_branch(tenant_a, [{"present": 1, "absent": 4, "grade": 30}] * 2)  # < MIN_BRANCH_CELL (3)

    row = _row(director, tiny.id)
    assert row["suppressed"] is True
    assert row["active_students"] == 2  # only the headcount survives
    assert row["score"] is None
    assert row["at_risk"] is None
    assert row["attendance_rate"] is None


def test_branch_with_no_academic_signal_is_unranked(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    scored = _make_branch(tenant_a, [{"present": 5, "grade": 90}] * 3)
    nodata = _make_branch(tenant_a, [{"grade": None}] * 3)  # 3 students, no attendance, no grades

    row = _row(director, nodata.id)
    assert row["suppressed"] is False
    assert row["active_students"] == 3
    assert row["attendance_rate"] is None
    assert row["avg_grade_pct"] is None
    assert row["score"] is None  # not handed a spurious score
    # the unranked branch sorts after the scored one
    assert row["rank"] > _row(director, scored.id)["rank"]


def test_grade_metric_requires_three_distinct_contributors(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    branch = _make_branch(
        tenant_a,
        [{"grade": 91}, {"grade": None}, {"grade": None}],
    )

    row = _row(director, branch.id)

    assert row["active_students"] == 3
    assert row["avg_grade_pct"] is None
    assert row["score"] is None


def test_attendance_metric_requires_three_distinct_contributors(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    branch = _make_branch(
        tenant_a,
        [{"present": 4}, {}, {}],
    )

    row = _row(director, branch.id)

    assert row["active_students"] == 3
    assert row["attendance_rate"] is None
    assert row["score"] is None


def test_branch_grade_and_risk_are_averaged_across_students(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    # three students all present; grades 90/30/60 -> avg 60; only the 30 is at-risk
    mixed = _make_branch(
        tenant_a,
        [{"present": 5, "grade": 90}, {"present": 5, "grade": 30}, {"present": 5, "grade": 60}],
    )
    row = _row(director, mixed.id)
    assert row["avg_grade_pct"] == 60.0  # mean across the students, not a single one
    assert row["at_risk"] == 1
    assert row["at_risk_rate"] == 0.333
    # 1.0*50 + 0.6*30 + (1 - 1/3)*20 = 81.3
    assert row["score"] == 81.3


def test_late_counts_as_attended_and_excused_is_excluded(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    # per student: 3 present + 1 late + 1 excused + 1 absent -> 4 attended / 5 non-excused
    branch = _make_branch(tenant_a, [{"present": 3, "late": 1, "excused": 1, "absent": 1, "grade": 90}] * 3)
    assert _row(director, branch.id)["attendance_rate"] == 0.8


def test_branch_ranking_scoped_to_membership(tenant_a, as_role, user_in, as_user):
    good = _make_branch(tenant_a, [{"present": 5, "grade": 90}] * 3)
    poor = _make_branch(tenant_a, [{"present": 1, "absent": 4, "grade": 30}] * 3)
    hod = as_user(tenant_a, user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=good))

    ids = {r["branch"] for r in hod.get(BRANCHES).json()["data"]["results"]}
    assert good.id in ids
    assert poor.id not in ids  # a branch-scoped manager sees only their own branch


def test_finance_signal_is_gated_out_of_overdue_and_at_risk(tenant_a, as_role, user_in, as_user):
    # students healthy on attendance + grades, at-risk ONLY via the overdue invoice
    branch = _make_branch(tenant_a, [{"present": 5, "grade": 90, "overdue": True}] * 3)
    director, _ = as_role(Role.DIRECTOR)  # holds finance:read via *:*
    teacher = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))

    drow, trow = _row(director, branch.id), _row(teacher, branch.id)
    # the finance-capable director: overdue counted, all at-risk, 1.0*50+0.9*30+(1-1)*20 = 77.0
    assert drow["overdue_students"] == 3
    assert drow["at_risk"] == 3
    assert drow["score"] == 77.0
    # a teacher without finance:read sees neither the overdue count nor overdue-driven risk
    assert trow["overdue_students"] is None
    assert trow["at_risk"] == 0
    assert trow["score"] == 97.0  # 50 + 27 + 20, no risk subtracted


def test_branch_ranking_method_is_transparent(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    method = director.get(BRANCHES).json()["data"]["method"]
    assert method["metrics"]["attendance_rate"]["weight"] == 50
    assert method["score_range"] == "0-100"
    assert method["min_cell_size"] == 3
    assert method["includes_finance"] is True  # director can see finance


def test_branch_ranking_denied_without_intelligence(tenant_a, as_role):
    cashier, _ = as_role(Role.CASHIER)  # holds no intelligence:read
    assert cashier.get(BRANCHES).status_code == 403
