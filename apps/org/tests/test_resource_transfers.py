from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.exceptions import ValidationException
from core.permissions import Role


@pytest.mark.django_db
def test_generalized_transfer_endpoint_accepts_the_web_app_contract(tenant_a, client_for):
    """Exercise the exact human-facing payload used by the CEO/staff consoles."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.services import create_staff_account
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from core.session_auth import create_session

    with schema_context(tenant_a.schema_name):
        source = BranchFactory(name="Transfer source")
        target = BranchFactory(name="Transfer destination")
        department = DepartmentFactory(branch=target)
        actor = create_staff_account(
            branch=source,
            role=Role.DIRECTOR,
            username="transfer.contract.director",
        )
        teacher = TeacherProfileFactory(branch=source)
        staff = create_staff_account(
            branch=source,
            role=Role.CASHIER,
            username="transfer.contract.cashier",
        )
        cohort = CohortFactory(branch=source, name="Transfer contract group")
        session = create_session(
            actor.user,
            principal_kind="staff",
            principal_id=actor.pk,
        )

    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
    cases = (
        (
            {
                "subject_kind": "teacher",
                "subject": teacher.pk,
                "to_branch": target.pk,
                "to_department": department.pk,
                "reason": "Teaching allocation",
                "confirm_impacts": True,
            },
            "teacher",
            teacher.pk,
        ),
        (
            {
                "subject_kind": "staff",
                "subject": staff.pk,
                "from_branch": source.pk,
                "to_branch": target.pk,
                "to_department": department.pk,
                "reason": "Operational coverage",
                "confirm_impacts": True,
            },
            "staff",
            staff.pk,
        ),
        (
            {
                "subject_kind": "cohort",
                "subject": cohort.pk,
                "to_branch": target.pk,
                "to_department": department.pk,
                "reason": "Campus relocation",
                "confirm_impacts": True,
            },
            "cohort",
            cohort.pk,
        ),
    )

    for body, expected_kind, expected_id in cases:
        response = client.post("/api/v1/org/transfers/", body, format="json")
        assert response.status_code == 201, response.content
        payload = response.json()["data"]
        assert payload["subject_kind"] == expected_kind
        assert payload["subject_id"] == expected_id
        assert payload["from_branch"] == source.pk
        assert payload["to_branch"] == target.pk


@pytest.mark.django_db
def test_teacher_transfer_moves_scope_and_detaches_source_groups(tenant_a):
    from apps.cohorts.tests.factories import CohortFactory, CohortTeacherFactory
    from apps.org.models import BranchTransfer
    from apps.org.services import transfer_teacher
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.services import ensure_role_membership

    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        target_department = DepartmentFactory(branch=target)
        teacher = TeacherProfileFactory(branch=source, user__first_name="Nodira")
        ensure_role_membership(teacher, branch=source, role=Role.TEACHER)
        cohort = CohortFactory(branch=source, primary_teacher=teacher)
        CohortTeacherFactory(cohort=cohort, teacher=teacher)

        with pytest.raises(ValidationException) as exc_info:
            transfer_teacher(
                teacher_id=teacher.pk,
                to_branch_id=target.pk,
                to_department_id=target_department.pk,
                allowed_branch_ids=None,
            )
        assert exc_info.value.code == "transfer_confirmation_required"

        transfer = transfer_teacher(
            teacher_id=teacher.pk,
            to_branch_id=target.pk,
            to_department_id=target_department.pk,
            reason="New teaching assignment",
            confirm_impacts=True,
            allowed_branch_ids=None,
        )

        teacher.refresh_from_db()
        cohort.refresh_from_db()
        assert teacher.branch_id == target.pk
        assert teacher.department_id == target_department.pk
        assert cohort.primary_teacher_id is None
        assert not cohort.co_teachers.filter(teacher=teacher).exists()
        assert RoleMembership.objects.filter(
            user=teacher.user,
            branch=target,
            department=target_department,
            revoked_at__isnull=True,
        ).exists()
        assert transfer.subject_kind == BranchTransfer.SubjectKind.TEACHER
        assert transfer.subject_id == teacher.pk
        assert transfer.user_id == teacher.user_id


@pytest.mark.django_db
def test_staff_transfer_moves_all_source_responsibilities(tenant_a):
    from apps.org.models import BranchTransfer
    from apps.org.services import create_staff_account, transfer_staff
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        staff = create_staff_account(
            branch=source,
            role=Role.CASHIER,
            username="moving.cashier",
            first_name="Malika",
        )

        transfer = transfer_staff(
            staff_id=staff.pk,
            from_branch_id=source.pk,
            to_branch_id=target.pk,
            reason="Branch coverage change",
            allowed_branch_ids=None,
        )

        assert not RoleMembership.objects.filter(
            user=staff.user,
            branch=source,
            revoked_at__isnull=True,
        ).exists()
        assert RoleMembership.objects.filter(
            user=staff.user,
            branch=target,
            revoked_at__isnull=True,
        ).exists()
        assert transfer.subject_kind == BranchTransfer.SubjectKind.STAFF
        assert transfer.subject_id == staff.pk


@pytest.mark.django_db
def test_group_transfer_carries_active_students_and_keeps_membership(tenant_a):
    from apps.cohorts.models import CohortMembership
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.models import BranchTransfer
    from apps.org.services import transfer_cohort
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.services import ensure_role_membership

    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        target_department = DepartmentFactory(branch=target)
        cohort = CohortFactory(branch=source, name="Teens Evening")
        student = StudentProfileFactory(branch=source, current_cohort=cohort)
        membership = CohortMembership.objects.create(
            cohort=cohort,
            student=student,
            start_date="2026-08-01",
        )
        ensure_role_membership(student, branch=source, role=Role.STUDENT)

        with pytest.raises(ValidationException) as exc_info:
            transfer_cohort(
                cohort_id=cohort.pk,
                to_branch_id=target.pk,
                to_department_id=target_department.pk,
                allowed_branch_ids=None,
            )
        assert exc_info.value.code == "transfer_confirmation_required"

        group_transfer = transfer_cohort(
            cohort_id=cohort.pk,
            to_branch_id=target.pk,
            to_department_id=target_department.pk,
            reason="New campus",
            confirm_impacts=True,
            allowed_branch_ids=None,
        )

        cohort.refresh_from_db()
        student.refresh_from_db()
        membership.refresh_from_db()
        assert cohort.branch_id == target.pk
        assert cohort.department_id == target_department.pk
        assert student.branch_id == target.pk
        assert student.current_cohort_id == cohort.pk
        assert membership.end_date is None
        assert group_transfer.subject_kind == BranchTransfer.SubjectKind.COHORT
        assert group_transfer.user_id is None
        assert BranchTransfer.objects.filter(
            subject_kind=BranchTransfer.SubjectKind.STUDENT,
            subject_id=student.pk,
            from_branch=source,
            to_branch=target,
        ).exists()
