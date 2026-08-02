"""Adversarial assignment permission-to-membership scope regressions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db


def _account_type(*, name: str, slug: str, kind: str, permissions: tuple[str, ...]):
    from apps.access.models import AccountType, AccountTypePermission

    account_type = AccountType.objects.create(
        name=name,
        slug=slug,
        account_kind=kind,
    )
    AccountTypePermission.objects.bulk_create(
        [
            AccountTypePermission(account_type=account_type, permission=permission)
            for permission in permissions
        ]
    )
    return account_type


def test_teacher_assignment_cannot_borrow_grant_from_another_branch(tenant_a, as_user):
    from apps.access.models import AccountType
    from apps.assignments.models import Assignment
    from apps.assignments.tests.factories import AssignmentFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        allowed_branch = BranchFactory()
        unrelated_branch = BranchFactory()
        assignment_teacher = _account_type(
            name="Scoped assignment teacher",
            slug="scoped-assignment-teacher",
            kind=AccountType.AccountKind.TEACHER,
            permissions=("assignments:read", "assignments:write"),
        )
        unrelated_teacher = _account_type(
            name="Unrelated teacher identity",
            slug="unrelated-teacher-identity",
            kind=AccountType.AccountKind.TEACHER,
            permissions=(),
        )
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=allowed_branch,
            account_type=assignment_teacher,
            role=assignment_teacher.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=unrelated_branch,
            account_type=unrelated_teacher,
            role=unrelated_teacher.compatibility_role,
        )
        teacher = TeacherProfileFactory(user=actor, branch=allowed_branch)
        allowed_cohort = CohortFactory(branch=allowed_branch, primary_teacher=teacher)
        # Deliberately malformed cross-branch teaching relationship. It is data,
        # not an authorization grant.
        unrelated_cohort = CohortFactory(branch=unrelated_branch, primary_teacher=teacher)
        allowed_assignment = AssignmentFactory(
            cohort=allowed_cohort,
            status=Assignment.Status.DRAFT,
        )
        unrelated_assignment = AssignmentFactory(
            cohort=unrelated_cohort,
            status=Assignment.Status.DRAFT,
        )
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    listing = client.get("/api/v1/assignments/")
    assert listing.status_code == 200, listing.content
    assert {row["id"] for row in listing.json()["data"]} == {allowed_assignment.pk}
    assert client.get(f"/api/v1/assignments/{unrelated_assignment.pk}/").status_code == 404

    denied_create = client.post(
        "/api/v1/assignments/",
        {
            "cohort": unrelated_cohort.pk,
            "title": "Borrowed branch",
            "due_at": (timezone.now() + timedelta(days=2)).isoformat(),
            "max_score": "100",
        },
        format="json",
    )
    assert denied_create.status_code == 400
    assert denied_create.json()["code"] == "validation_error"
    assert (
        client.patch(
            f"/api/v1/assignments/{unrelated_assignment.pk}/",
            {"title": "Borrowed write"},
            format="json",
        ).status_code
        == 404
    )


def test_read_scope_cannot_lend_visibility_to_write_scope(tenant_a, as_user):
    from apps.access.models import AccountType
    from apps.assignments.models import Assignment
    from apps.assignments.tests.factories import AssignmentFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        read_branch = BranchFactory()
        write_branch = BranchFactory()
        reader = _account_type(
            name="Assignment reader",
            slug="assignment-reader-split-scope",
            kind=AccountType.AccountKind.STAFF,
            permissions=("assignments:read",),
        )
        writer = _account_type(
            name="Assignment writer",
            slug="assignment-writer-split-scope",
            kind=AccountType.AccountKind.STAFF,
            permissions=("assignments:write",),
        )
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=read_branch,
            account_type=reader,
            role=reader.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=write_branch,
            account_type=writer,
            role=writer.compatibility_role,
        )
        readable = AssignmentFactory(
            cohort=CohortFactory(branch=read_branch),
            status=Assignment.Status.DRAFT,
        )
        writable = AssignmentFactory(
            cohort=CohortFactory(branch=write_branch),
            status=Assignment.Status.DRAFT,
        )
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    listing = client.get("/api/v1/assignments/")
    assert {row["id"] for row in listing.json()["data"]} == {readable.pk}
    assert (
        client.patch(
            f"/api/v1/assignments/{readable.pk}/",
            {"title": "Read borrowed by write"},
            format="json",
        ).status_code
        == 404
    )
    allowed = client.patch(
        f"/api/v1/assignments/{writable.pk}/",
        {"title": "Exact write scope"},
        format="json",
    )
    assert allowed.status_code == 200, allowed.content


def test_student_write_grant_does_not_convert_enrollment_into_authoring_scope(tenant_a, as_user):
    """Account-type configuration cannot broaden a student's natural relationship."""
    from apps.access.models import AccountType
    from apps.assignments.models import Assignment
    from apps.assignments.tests.factories import AssignmentFactory
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        malformed_writer = _account_type(
            name="Malformed student writer",
            slug="malformed-student-writer",
            kind=AccountType.AccountKind.STUDENT,
            permissions=("assignments:read", "assignments:write"),
        )
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=branch,
            account_type=malformed_writer,
            role=malformed_writer.compatibility_role,
        )
        student = StudentProfileFactory(user=actor, branch=branch)
        cohort = CohortFactory(branch=branch)
        CohortMembershipFactory(cohort=cohort, student=student)
        assignment = AssignmentFactory(
            cohort=cohort,
            status=Assignment.Status.PUBLISHED,
        )
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    assert client.get(f"/api/v1/assignments/{assignment.pk}/").status_code == 200
    assert (
        client.patch(
            f"/api/v1/assignments/{assignment.pk}/",
            {"title": "Enrollment is not authoring authority"},
            format="json",
        ).status_code
        == 404
    )
