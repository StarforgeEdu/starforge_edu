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
    teacher, teacher_user = as_role(Role.TEACHER)  # staff, but no finance:read and not the family
    with schema_context(tenant_a.schema_name):
        teacher_branch = teacher_user.role_memberships.get(role=Role.TEACHER).branch
    student = _student_with_events(tenant_a, teacher_branch)

    types = {e["type"] for e in teacher.get(_journey_url(student.id)).json()["data"]["events"]}
    assert "grade" in types  # the academic story is visible
    assert "invoice" not in types  # ...but not the family's billing


def test_journey_cannot_borrow_finance_grant_from_another_branch(
    tenant_a,
    user_in,
    client_for,
):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.users.models import RoleMembership
    from tests.role_principal_helpers import ensure_role_principal, exact_session_client

    student_branch = _branch(tenant_a)
    finance_branch = _branch(tenant_a)
    student = _student_with_events(tenant_a, student_branch)
    with schema_context(tenant_a.schema_name):
        student_reader = AccountType.objects.create(
            name="Journey reader",
            slug="journey-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        finance_reader = AccountType.objects.create(
            name="Remote finance reader",
            slug="remote-finance-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=student_reader,
            permission="students:read",
        )
        AccountTypePermission.objects.create(
            account_type=finance_reader,
            permission="finance:read",
        )
        viewer = user_in(tenant_a)
        staff = ensure_role_principal(viewer, roles=[Role.SUPPORT], branch=student_branch)
        RoleMembership.objects.create(
            user=viewer,
            branch=student_branch,
            role=student_reader.compatibility_role,
            account_type=student_reader,
        )
        RoleMembership.objects.create(
            user=viewer,
            branch=finance_branch,
            role=finance_reader.compatibility_role,
            account_type=finance_reader,
        )
        viewer.refresh_from_db()

    client = exact_session_client(
        client_for,
        tenant_a,
        viewer,
        principal_kind="staff",
        principal_id=staff.pk,
    )
    response = client.get(_journey_url(student.pk))

    assert response.status_code == 200
    assert "invoice" not in {event["type"] for event in response.json()["data"]["events"]}


def test_staff_session_on_shared_student_bridge_is_not_treated_as_family(
    tenant_a,
    client_for,
):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.org.models import StaffProfile
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory
    from tests.role_principal_helpers import exact_session_client

    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        shared_user = UserFactory()
    student = _student_with_events(tenant_a, branch, user=shared_user)
    with schema_context(tenant_a.schema_name):
        staff = StaffProfile.objects.create(
            user=shared_user,
            username=f"staff-{shared_user.username}",
            password=shared_user.password,
        )
        account_type = AccountType.objects.create(
            name="Student records only",
            slug="student-records-only",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=account_type,
            permission="students:read",
        )
        RoleMembership.objects.create(
            user=shared_user,
            branch=branch,
            role=account_type.compatibility_role,
            account_type=account_type,
        )
        shared_user.refresh_from_db()

    client = exact_session_client(
        client_for,
        tenant_a,
        shared_user,
        principal_kind="staff",
        principal_id=staff.pk,
    )
    response = client.get(_journey_url(student.pk))

    assert response.status_code == 200
    assert "invoice" not in {event["type"] for event in response.json()["data"]["events"]}


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


def test_journey_is_capped_at_one_hundred_events_and_supports_head(tenant_a, as_role):
    from apps.students.models import EnrollmentEvent
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create(branch=branch)
        EnrollmentEvent.objects.bulk_create(
            [
                EnrollmentEvent(student=student, from_status="lead", to_status="active", note=str(i))
                for i in range(101)
            ]
        )

    url = _journey_url(student.id)
    response = director.get(url)
    assert response.status_code == 200
    assert len(response.json()["data"]["events"]) == 100
    assert director.head(url).status_code == 200
