"""A-3 facet — family-health retention feed: each family (a guardian + the children
they guard) flagged good/watch/at_risk, worst first; gated to the retention desk."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

FAMILIES = "/api/v1/intelligence/families/"


def _family(tenant, branch, children):
    """A parent guarding one student per entry in `children`. Each child gets its own
    cohort + teacher (no lesson-overlap). child keys: present/absent (marks), grade
    (score or None), overdue (bool). Returns the parent."""
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.tests.factories import CohortFactory
    from apps.finance.models import Invoice
    from apps.finance.tests.factories import InvoiceFactory
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    St = AttendanceRecord.Status
    with schema_context(tenant.schema_name):
        parent = ParentProfileFactory.create()
        term = TermFactory.create()
        base = timezone.now() - timedelta(days=2)
        for child in children:
            cohort = CohortFactory.create(branch=branch)
            teacher = TeacherProfileFactory.create(branch=branch)
            student = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
            GuardianFactory.create(parent=parent, student=student, is_primary=True)
            marks = [St.PRESENT] * child.get("present", 0) + [St.ABSENT] * child.get("absent", 0)
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
            if child.get("grade") is not None:
                exam = ExamFactory.create(is_published=True, cohort=cohort)
                ExamResultFactory.create(exam=exam, student=student, score=Decimal(str(child["grade"])))
            if child.get("overdue"):
                InvoiceFactory.create(student=student, status=Invoice.Status.OVERDUE)
    return parent


def _branch(tenant):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        return BranchFactory.create()


def test_family_health_levels_and_order(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)  # holds parents:read + finance via *:*
    branch = _branch(tenant_a)
    good = _family(tenant_a, branch, [{"present": 5, "grade": 90}, {"present": 5, "grade": 90}])
    watch = _family(
        tenant_a,
        branch,
        [{"present": 5, "grade": 30}, {"present": 5, "grade": 90}, {"present": 5, "grade": 90}],
    )  # 1 of 3 children at-risk -> 0.33 -> watch
    at_risk = _family(tenant_a, branch, [{"present": 5, "grade": 90, "overdue": True}])  # overdue -> at_risk

    body = director.get(FAMILIES).json()["data"]
    rows = {r["family"]: r for r in body["results"]}
    assert rows[good.id]["health"] == "good"
    assert rows[good.id]["at_risk_children"] == 0
    assert rows[watch.id]["health"] == "watch"
    assert rows[watch.id]["at_risk_children"] == 1
    assert rows[at_risk.id]["health"] == "at_risk"
    assert rows[at_risk.id]["overdue_children"] == 1
    # worst families surface first
    positions = {r["family"]: i for i, r in enumerate(body["results"])}
    assert positions[at_risk.id] < positions[watch.id] < positions[good.id]


def test_family_health_overdue_is_finance_gated(tenant_a, as_role, user_in, as_user):
    branch = _branch(tenant_a)
    # a family whose only issue is an overdue invoice (child is otherwise healthy)
    family = _family(tenant_a, branch, [{"present": 5, "grade": 90, "overdue": True}])
    director, _ = as_role(Role.DIRECTOR)
    registrar = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))

    drow = next(r for r in director.get(FAMILIES).json()["data"]["results"] if r["family"] == family.id)
    rrow = next(r for r in registrar.get(FAMILIES).json()["data"]["results"] if r["family"] == family.id)
    # finance-capable director sees the overdue-driven risk
    assert drow["health"] == "at_risk"
    assert drow["overdue_children"] == 1
    # reception (no finance:read) sees neither the overdue count nor overdue-driven risk
    assert rrow["overdue_children"] is None
    assert rrow["at_risk_children"] == 0
    assert rrow["health"] == "good"


def test_family_health_scoped_to_branch(tenant_a, user_in, as_user):
    home = _branch(tenant_a)
    other = _branch(tenant_a)
    mine = _family(tenant_a, home, [{"present": 5, "grade": 90}])
    theirs = _family(tenant_a, other, [{"present": 5, "grade": 90}])
    registrar = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=home))

    ids = {r["family"] for r in registrar.get(FAMILIES).json()["data"]["results"]}
    assert mine.id in ids
    assert theirs.id not in ids  # only families with children in the caller's branch


def test_family_health_denied_for_non_retention_roles(tenant_a, as_role):
    # a teacher has intelligence:read but NOT parents:read -> blocked at the 2nd gate
    teacher, _ = as_role(Role.TEACHER)
    assert teacher.get(FAMILIES).status_code == 403
    # a parent has parents:read but NOT intelligence:read -> blocked at the 1st gate
    # (the symmetric half: a parent must never see other families)
    parent, _ = as_role(Role.PARENT)
    assert parent.get(FAMILIES).status_code == 403
    # a cashier lacks both -> blocked at the first gate
    cashier, _ = as_role(Role.CASHIER)
    assert cashier.get(FAMILIES).status_code == 403


def test_family_spanning_branches_is_branch_scoped(tenant_a, as_role, user_in, as_user):
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    home = _branch(tenant_a)
    other = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        parent = ParentProfileFactory.create()  # one child in each branch
        for branch in (home, other):
            student = StudentProfileFactory.create(branch=branch)
            GuardianFactory.create(parent=parent, student=student, is_primary=True)
    director, _ = as_role(Role.DIRECTOR)
    registrar = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=home))

    drow = next(r for r in director.get(FAMILIES).json()["data"]["results"] if r["family"] == parent.id)
    rrow = next(r for r in registrar.get(FAMILIES).json()["data"]["results"] if r["family"] == parent.id)
    assert drow["children"] == 2  # the director sees both branches' children
    assert rrow["children"] == 1  # the home registrar sees only the in-scope child


def test_all_withdrawn_family_is_omitted(tenant_a, as_role):
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        parent = ParentProfileFactory.create()
        student = StudentProfileFactory.create(branch=branch, status=StudentProfile.Status.WITHDRAWN)
        GuardianFactory.create(parent=parent, student=student, is_primary=True)
    director, _ = as_role(Role.DIRECTOR)
    ids = {r["family"] for r in director.get(FAMILIES).json()["data"]["results"]}
    assert parent.id not in ids  # an already-churned family drops off the retention feed


def test_child_shared_by_two_guardians_counts_in_both(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(branch=branch)
        student = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
        exam = ExamFactory.create(is_published=True, cohort=cohort)
        ExamResultFactory.create(exam=exam, student=student, score=Decimal("30"))  # low grade -> at-risk
        p1 = ParentProfileFactory.create()
        p2 = ParentProfileFactory.create()
        GuardianFactory.create(parent=p1, student=student, is_primary=True)
        GuardianFactory.create(parent=p2, student=student, is_primary=False)
    director, _ = as_role(Role.DIRECTOR)
    rows = {r["family"]: r for r in director.get(FAMILIES).json()["data"]["results"]}
    # separated parents: the shared at-risk child flags BOTH families (both get the call)
    assert rows[p1.id]["at_risk_children"] == 1
    assert rows[p2.id]["at_risk_children"] == 1
