"""A-3 facet — student journey timeline: one student's story (enrollment, grades,
achievements, finance-gated invoices) in one chronological feed, newest first."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db


def _journey_url(student_id):
    return f"/api/v1/intelligence/journey/{student_id}/"


def _student_with_events(tenant, branch, *, user=None):
    """A student with one of each event type: an enrollment move, a published grade,
    an achievement, and an invoice."""
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.achievements.models import Achievement, AchievementGrant
    from apps.cohorts.tests.factories import CohortFactory
    from apps.finance.models import Invoice
    from apps.finance.tests.factories import InvoiceFactory
    from apps.students.models import EnrollmentEvent
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        cohort = CohortFactory.create(branch=branch)
        kwargs = {"user": user} if user is not None else {}
        student = StudentProfileFactory.create(branch=branch, current_cohort=cohort, **kwargs)
        EnrollmentEvent.objects.create(student=student, from_status="lead", to_status="active")
        exam = ExamFactory.create(is_published=True, cohort=cohort)
        ExamResultFactory.create(exam=exam, student=student, score=Decimal("88"))
        ach = Achievement.objects.create(
            name="Top of class",
            scope=Achievement.Scope.GLOBAL,
            status=Achievement.Status.ACTIVE,
            branch=branch,
        )
        AchievementGrant.objects.create(achievement=ach, student=student)
        InvoiceFactory.create(student=student, status=Invoice.Status.ISSUED)
    return student


def _branch(tenant):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        return BranchFactory.create()


def test_journey_merges_all_event_types_newest_first(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)  # finance-visible -> sees invoices too
    student = _student_with_events(tenant_a, _branch(tenant_a))

    body = director.get(_journey_url(student.id)).json()["data"]
    assert body["student"] == student.id
    types = {e["type"] for e in body["events"]}
    assert types == {"enrollment", "grade", "achievement", "invoice"}
    ats = [e["at"] for e in body["events"]]
    assert ats == sorted(ats, reverse=True)  # newest first


def test_journey_invoices_are_finance_gated(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)  # staff, but no finance:read and not the family
    student = _student_with_events(tenant_a, _branch(tenant_a))

    types = {e["type"] for e in teacher.get(_journey_url(student.id)).json()["data"]["events"]}
    assert "grade" in types  # the academic story is visible
    assert "invoice" not in types  # ...but not the family's billing


def test_student_sees_own_journey_including_invoices(tenant_a, user_in, as_user):
    branch = _branch(tenant_a)
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    student = _student_with_events(tenant_a, branch, user=student_user)
    client = as_user(tenant_a, student_user)

    types = {e["type"] for e in client.get(_journey_url(student.id)).json()["data"]["events"]}
    assert "invoice" in types  # a student sees their OWN bills


def test_student_cannot_see_another_students_journey(tenant_a, user_in, as_user):
    branch = _branch(tenant_a)
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    _student_with_events(tenant_a, branch, user=student_user)  # the requester's own profile
    other = _student_with_events(tenant_a, branch)
    client = as_user(tenant_a, student_user)

    assert client.get(_journey_url(other.id)).status_code == 404  # out of scope


def test_out_of_scope_role_gets_404(tenant_a, as_role):
    # a cashier is staff but not a student-facing role -> scoped_students is empty
    cashier, _ = as_role(Role.CASHIER)
    student = _student_with_events(tenant_a, _branch(tenant_a))
    assert cashier.get(_journey_url(student.id)).status_code == 404


def test_it_role_cannot_read_a_journey(tenant_a, as_role):
    # IT is a STAFF_ROLE (scoped_students returns all students) but holds no
    # students:read — it is walled off academic data everywhere else, and here too
    it, _ = as_role(Role.IT)
    student = _student_with_events(tenant_a, _branch(tenant_a))
    assert it.get(_journey_url(student.id)).status_code == 404


def test_guardian_sees_own_childs_journey_including_invoices(tenant_a, user_in, as_user):
    from apps.parents.models import ParentProfile
    from apps.parents.tests.factories import GuardianFactory

    branch = _branch(tenant_a)
    parent_user = user_in(tenant_a, roles=[Role.PARENT], branch=branch)
    student = _student_with_events(tenant_a, branch)
    other = _student_with_events(tenant_a, branch)  # a different family
    with schema_context(tenant_a.schema_name):
        parent_profile = ParentProfile.objects.create(user=parent_user)
        GuardianFactory.create(parent=parent_profile, student=student, is_primary=True)
    client = as_user(tenant_a, parent_user)

    types = {e["type"] for e in client.get(_journey_url(student.id)).json()["data"]["events"]}
    assert "invoice" in types  # a guardian sees their own child's bills
    assert client.get(_journey_url(other.id)).status_code == 404  # but not another family's


def test_unpublished_grades_are_excluded(tenant_a, as_role):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(branch=branch)
        student = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
        published = ExamFactory.create(is_published=True, cohort=cohort)
        ExamResultFactory.create(exam=published, student=student, score=Decimal("90"))
        draft = ExamFactory.create(is_published=False, cohort=cohort)
        ExamResultFactory.create(exam=draft, student=student, score=Decimal("40"))

    events = director.get(_journey_url(student.id)).json()["data"]["events"]
    grades = [e for e in events if e["type"] == "grade"]
    assert len(grades) == 1  # only the PUBLISHED grade — a draft mark never leaks


def test_empty_student_returns_empty_feed(tenant_a, as_role):
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create(branch=branch)
    body = director.get(_journey_url(student.id)).json()["data"]
    assert body["student"] == student.id
    assert body["events"] == []


def test_journey_orders_by_timestamp_not_source_order(tenant_a, as_role):
    from apps.students.models import EnrollmentEvent

    director, _ = as_role(Role.DIRECTOR)
    student = _student_with_events(tenant_a, _branch(tenant_a))
    # the enrollment event is created FIRST (oldest by source order); force its
    # timestamp to be the newest and assert the selector sorts by the real time
    with schema_context(tenant_a.schema_name):
        EnrollmentEvent.objects.filter(student=student).update(created_at=timezone.now() + timedelta(days=1))
    events = director.get(_journey_url(student.id)).json()["data"]["events"]
    assert events[0]["type"] == "enrollment"  # newest by timestamp, despite source order
