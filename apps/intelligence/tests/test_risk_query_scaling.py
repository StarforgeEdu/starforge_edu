"""Regression coverage for the production-sized intelligence query shape.

The peak-center fixture exposed a failure that tiny factory tests could not: the
risk annotations were expanded repeatedly inside ``COUNT(FILTER(...))`` and the
page query.  PostgreSQL consequently rescanned lessons hundreds of millions of
times, exhausted both synchronous workers, and pushed the web cgroup into OOM.

These tests deliberately assert the database shape, not wall-clock timing.  A
CI runner's speed is noisy; a bounded number of set-based queries and a plan
without correlated/repeated subplans are deterministic properties.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.utils import schema_context

from core.historical_scope import ScopeAttributionStatus

pytestmark = [pytest.mark.django_db, pytest.mark.slow]

_RISK_RELATIONS = {
    "academics_examresult",
    "attendance_attendancerecord",
    "finance_invoice",
    "schedule_lesson",
    "students_studentprofile",
}


def _build_population(tenant) -> dict[str, Any]:
    """Create 96 students and enough signals to exercise realistic planner paths.

    Each of two branches has 48 active students split over two cohorts.  Every
    student has twelve attendance marks and one published result; one quarter
    has low attendance, one quarter a low grade, one quarter overdue debt, and
    one quarter is healthy.  Signals are intentionally disjoint so finance
    permission and branch-scope assertions remain exact.
    """

    from apps.academics.integrity import assessment_integrity_write
    from apps.academics.models import Exam, ExamResult
    from apps.academics.tests.factories import SubjectFactory
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.tests.factories import CohortFactory
    from apps.finance.models import Invoice
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    now = timezone.now()
    today = timezone.localdate(now)
    with schema_context(tenant.schema_name):
        term = TermFactory(name="Risk scaling term")
        subject = SubjectFactory(name="English", code="risk-scale-english")
        branches: list[Any] = []
        students_by_branch: dict[int, list[Any]] = {}
        signal_ids_by_branch: dict[int, dict[str, set[int]]] = {}

        for branch_number in range(2):
            branch = BranchFactory(
                name=f"Risk Scale Branch {branch_number}",
                slug=f"risk-scale-branch-{branch_number}",
            )
            department = DepartmentFactory(
                branch=branch,
                name=f"Risk Scale English {branch_number}",
                slug=f"risk-scale-english-{branch_number}",
            )
            teacher = TeacherProfileFactory(branch=branch, department=department)
            branch_students: list[Any] = []
            signals = {"attendance": set(), "grade": set(), "overdue": set()}

            for cohort_number in range(2):
                cohort = CohortFactory(
                    branch=branch,
                    department=department,
                    name=f"Risk Scale {branch_number}-{cohort_number}",
                )
                cohort_students = StudentProfileFactory.create_batch(
                    24,
                    branch=branch,
                    current_cohort=cohort,
                )
                branch_students.extend(cohort_students)

                lessons = Lesson.objects.bulk_create(
                    [
                        Lesson(
                            term=term,
                            cohort=cohort,
                            teacher=teacher,
                            title=f"Risk scale lesson {lesson_number}",
                            starts_at=now - timedelta(days=12 - lesson_number),
                            ends_at=now - timedelta(days=12 - lesson_number) + timedelta(hours=1),
                            status=Lesson.Status.COMPLETED,
                        )
                        for lesson_number in range(12)
                    ]
                )

                attendance_rows: list[AttendanceRecord] = []
                for student_number, student in enumerate(cohort_students):
                    signal_number = student_number % 4
                    if signal_number == 0:
                        signals["attendance"].add(student.pk)
                    elif signal_number == 1:
                        signals["grade"].add(student.pk)
                    elif signal_number == 2:
                        signals["overdue"].add(student.pk)
                    for lesson_number, lesson in enumerate(lessons):
                        status = AttendanceRecord.Status.PRESENT
                        if signal_number == 0 and lesson_number < 6:
                            status = AttendanceRecord.Status.ABSENT
                        attendance_rows.append(
                            AttendanceRecord(student=student, lesson=lesson, status=status)
                        )
                AttendanceRecord.objects.bulk_create(attendance_rows, batch_size=500)

                exam = Exam.objects.create(
                    subject=subject,
                    cohort=cohort,
                    term=term,
                    title="Risk scale published exam",
                    exam_date=today - timedelta(days=3),
                    max_score=Decimal("100"),
                    is_published=True,
                    published_at=now - timedelta(days=2),
                )
                with assessment_integrity_write():
                    ExamResult.objects.bulk_create(
                        [
                            ExamResult(
                                exam=exam,
                                student=student,
                                score=(Decimal("30") if student.pk in signals["grade"] else Decimal("85")),
                            )
                            for student in cohort_students
                        ]
                    )

            Invoice.objects.bulk_create(
                [
                    Invoice(
                        number=f"INV-RISK-SCALE-{branch_number}-{student.pk}",
                        student=student,
                        cohort=student.current_cohort,
                        branch_at_issue=branch,
                        department_at_issue=department,
                        attribution_status=ScopeAttributionStatus.CAPTURED,
                        status=Invoice.Status.OVERDUE,
                        issue_date=today - timedelta(days=4),
                        due_date=today - timedelta(days=1),
                        total_uzs=Decimal("1000000"),
                    )
                    for student in branch_students
                    if student.pk in signals["overdue"]
                ]
            )
            branches.append(branch)
            students_by_branch[branch.pk] = branch_students
            signal_ids_by_branch[branch.pk] = signals

    return {
        "branches": branches,
        "students": students_by_branch,
        "signals": signal_ids_by_branch,
        "now": now,
        "today": today,
    }


def _select_sql(capture: CaptureQueriesContext) -> list[str]:
    return [
        item["sql"] for item in capture.captured_queries if item["sql"].lstrip().upper().startswith("SELECT")
    ]


def _plan_nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _plan_nodes(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _plan_nodes(nested)


def _explain_nodes(sql: str) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (FORMAT JSON, COSTS OFF) {sql}")
        document = cursor.fetchone()[0]
    if isinstance(document, str):
        document = json.loads(document)
    return list(_plan_nodes(document[0]["Plan"]))


def _assert_set_based_risk_queries(capture: CaptureQueriesContext) -> None:
    selects = [
        sql
        for sql in _select_sql(capture)
        if any(
            f'"{relation}"' in sql
            for relation in (
                "academics_examresult",
                "attendance_attendancerecord",
                "students_studentprofile",
            )
        )
    ]
    assert selects, "The selector must obtain its result from the database."

    for sql in selects:
        compact = re.sub(r"\s+", " ", sql).upper()
        # ``OuterRef`` EXISTS was the production failure mode.  Set-based signal
        # queries may use an uncorrelated IN(SELECT scoped student ids), but must
        # never expand the same correlated signal repeatedly in count/page SQL.
        assert "EXISTS(" not in compact.replace(" ", ""), (
            "Risk SQL must not contain a correlated EXISTS subquery."
        )
        for relation in _RISK_RELATIONS:
            # Column references legitimately repeat a qualifier. Count relation
            # introductions instead: every duplicate FROM/JOIN is another scan
            # candidate, which is exactly what the former annotation expansion
            # produced.
            relation_pattern = rf'(?:FROM|JOIN)\s+"{re.escape(relation.upper())}"(?=\s)'
            occurrences = len(re.findall(relation_pattern, compact))
            # Invoice scope has two independent, set-based uses of StudentProfile:
            # immutable branch-at-issue consistency and the caller-authorized
            # student semi-join. They are not correlated subplans.
            maximum = 2 if relation == "students_studentprofile" else 1
            assert occurrences <= maximum, f"{relation} was introduced {occurrences} times in one query."

        nodes = _explain_nodes(sql)
        subplans = [
            node for node in nodes if node.get("Parent Relationship") == "SubPlan" or "Subplan Name" in node
        ]
        assert subplans == [], "Risk query plan contains a correlated SubPlan."

        relation_scans: dict[str, int] = {}
        for node in nodes:
            relation = node.get("Relation Name")
            if relation in _RISK_RELATIONS:
                relation_scans[relation] = relation_scans.get(relation, 0) + 1
        assert all(
            scan_count <= (2 if relation == "students_studentprofile" else 1)
            for relation, scan_count in relation_scans.items()
        ), f"Risk query repeats relation scans: {relation_scans}."


def _summary_context(population: dict[str, Any], branch: Any, *, include_finance: bool):
    from apps.intelligence.dto import (
        ExecutiveScopeBoundary,
        ExecutiveScopeLabel,
        ExecutiveSummaryContext,
        ExecutiveSummaryScope,
        ExecutiveSummaryWindow,
    )

    included = {"risk"}
    if include_finance:
        included.add("finance")
    today = population["today"]
    return ExecutiveSummaryContext(
        generated_at=population["now"],
        scope=ExecutiveSummaryScope(
            boundaries=(ExecutiveScopeBoundary(branch_id=branch.pk),),
            branches=(ExecutiveScopeLabel(id=branch.pk, name=branch.name),),
            departments=(),
            organization_wide=False,
            requested_branch_id=branch.pk,
            requested_department_id=None,
        ),
        window=ExecutiveSummaryWindow(
            date_from=today - timedelta(days=29),
            date_to=today,
            timezone="Asia/Tashkent",
        ),
        locale="en",
        currency="UZS",
        included_sections=frozenset(included),
        user_id=1,
        principal_kind="staff",
        principal_id=1,
    )


def test_risk_page_uses_bounded_set_queries_and_preserves_finance_scope(tenant_a):
    from apps.intelligence.selectors import student_risk_page
    from apps.students.models import StudentProfile

    population = _build_population(tenant_a)
    branch = population["branches"][0]
    signals = population["signals"][branch.pk]
    expected_without_finance = signals["attendance"] | signals["grade"]
    expected_with_finance = expected_without_finance | signals["overdue"]
    remote_student_ids = {student.pk for student in population["students"][population["branches"][1].pk]}

    with schema_context(tenant_a.schema_name):
        with CaptureQueriesContext(connection) as finance_capture:
            finance_rows, finance_total = student_risk_page(
                StudentProfile.objects.filter(branch=branch),
                include_finance=True,
                page=1,
                page_size=100,
                now=population["now"],
            )
        with CaptureQueriesContext(connection) as no_finance_capture:
            no_finance_rows, no_finance_total = student_risk_page(
                StudentProfile.objects.filter(branch=branch),
                include_finance=False,
                page=1,
                page_size=100,
                now=population["now"],
            )

        assert finance_total == 36
        assert no_finance_total == 24
        assert {row["student"] for row in finance_rows} == expected_with_finance
        assert {row["student"] for row in no_finance_rows} == expected_without_finance
        assert all(row["student"] not in remote_student_ids for row in finance_rows)
        overdue_only = next(row for row in finance_rows if row["student"] in signals["overdue"])
        assert {flag["code"] for flag in overdue_only["flags"]} == {"overdue_payment"}

        assert len(_select_sql(finance_capture)) <= 6
        assert len(_select_sql(no_finance_capture)) <= 5
        assert any('"finance_invoice"' in sql for sql in _select_sql(finance_capture))
        assert all('"finance_invoice"' not in sql for sql in _select_sql(no_finance_capture))
        _assert_set_based_risk_queries(finance_capture)
        _assert_set_based_risk_queries(no_finance_capture)


def test_executive_risk_summary_is_set_based_and_scope_exact(tenant_a):
    from apps.intelligence.selectors import executive_summary

    population = _build_population(tenant_a)
    branch = population["branches"][0]

    with schema_context(tenant_a.schema_name):
        with CaptureQueriesContext(connection) as no_finance_capture:
            no_finance = executive_summary(_summary_context(population, branch, include_finance=False))
        with CaptureQueriesContext(connection) as finance_capture:
            finance = executive_summary(_summary_context(population, branch, include_finance=True))

        assert no_finance["risk"] == {
            "student_sample_size": 48,
            "at_risk_students": 24,
            "high_risk_students": 0,
            "medium_risk_students": 12,
            "low_risk_students": 12,
            "low_attendance_students": 12,
            "low_grade_students": 12,
            "overdue_payment_students": 0,
            "at_risk_rate_fraction": 0.5,
            "included_signals": ["low_attendance", "low_grades"],
            "finance_signal_included": False,
        }
        assert finance["risk"] == {
            "student_sample_size": 48,
            "at_risk_students": 36,
            "high_risk_students": 0,
            "medium_risk_students": 12,
            "low_risk_students": 24,
            "low_attendance_students": 12,
            "low_grade_students": 12,
            "overdue_payment_students": 12,
            "at_risk_rate_fraction": 0.75,
            "included_signals": ["low_attendance", "low_grades", "overdue_payment"],
            "finance_signal_included": True,
        }

        assert len(_select_sql(no_finance_capture)) <= 5
        assert len(_select_sql(finance_capture)) <= 11
        assert all('"finance_invoice"' not in sql for sql in _select_sql(no_finance_capture))
        _assert_set_based_risk_queries(no_finance_capture)
        _assert_set_based_risk_queries(finance_capture)
