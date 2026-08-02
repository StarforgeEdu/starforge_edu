"""Adversarial organization-scope, privacy, and history regressions."""

from __future__ import annotations

import pytest
from django.db import DatabaseError, transaction
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db


def _scoped_account(*, branch, department=None, permissions: tuple[str, ...]):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    user = UserFactory()
    account_type = AccountType.objects.create(
        name=f"Scoped organization account {user.pk}",
        slug=f"scoped-org-{user.pk}",
        account_kind=AccountType.AccountKind.STAFF,
    )
    AccountTypePermission.objects.bulk_create(
        [AccountTypePermission(account_type=account_type, permission=code) for code in permissions]
    )
    RoleMembership.objects.create(
        user=user,
        branch=branch,
        department=department,
        role=Role.SUPPORT,
        account_type=account_type,
    )
    user.refresh_from_db()
    return user


def test_department_membership_does_not_expand_to_siblings(tenant_a, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        own = DepartmentFactory(branch=branch)
        sibling = DepartmentFactory(branch=branch)
        user = _scoped_account(branch=branch, department=own, permissions=("org:read",))
    client = as_user(tenant_a, user)

    response = client.get("/api/v1/org/departments/")
    assert response.status_code == 200, response.content
    assert {row["id"] for row in response.json()["data"]} == {own.pk}
    assert client.get(f"/api/v1/org/departments/{sibling.pk}/").status_code == 404


def test_department_budget_requires_independent_finance_scope(tenant_a, as_user, as_role):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch, budget="1250000.00")
        reader = _scoped_account(branch=branch, department=department, permissions=("org:read",))
        org_writer = _scoped_account(
            branch=branch,
            permissions=("org:read", "org:write"),
        )

    reader_response = as_user(tenant_a, reader).get(f"/api/v1/org/departments/{department.pk}/")
    assert reader_response.status_code == 200
    assert "budget" not in reader_response.json()["data"]

    denied = as_user(tenant_a, org_writer).patch(
        f"/api/v1/org/departments/{department.pk}/",
        {"budget": "1300000.00"},
        format="json",
    )
    assert denied.status_code == 403

    director, _user = as_role(Role.DIRECTOR)
    director_response = director.get(f"/api/v1/org/departments/{department.pk}/")
    assert director_response.status_code == 200
    assert director_response.json()["data"]["budget"] == "1250000.00"


@pytest.mark.parametrize(
    "changes",
    [
        {"currency_primary": "usd"},
        {"currency_secondary": "UZS"},
        {"disabled_apps": {"placement": True}},
        {"disabled_apps": ["placement", "placement"]},
        {"disabled_apps": ["not-a-real-application"]},
    ],
)
def test_center_settings_database_rejects_corrupt_policy_state(tenant_a, changes):
    from django.db import IntegrityError

    from apps.org.models import CenterSettings

    with (
        schema_context(tenant_a.schema_name),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        CenterSettings.objects.filter(pk=1).update(**changes)


def test_scoped_writers_cannot_mint_new_organization_scope(tenant_a, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        branch_writer = _scoped_account(branch=branch, permissions=("org:write",))
        department_writer = _scoped_account(
            branch=branch,
            department=department,
            permissions=("org:write",),
        )

    branch_client = as_user(tenant_a, branch_writer)
    assert (
        branch_client.post(
            "/api/v1/org/branches/",
            {"name": "Unauthorized", "slug": "unauthorized"},
            format="json",
        ).status_code
        == 403
    )
    department_client = as_user(tenant_a, department_writer)
    assert (
        department_client.post(
            "/api/v1/org/departments/",
            {"branch": branch.pk, "name": "Sibling", "slug": "sibling"},
            format="json",
        ).status_code
        == 403
    )
    assert (
        department_client.patch(
            f"/api/v1/org/branches/{branch.pk}/",
            {"name": "Department grant must not rename a branch"},
            format="json",
        ).status_code
        == 403
    )
    assert (
        department_client.put(
            f"/api/v1/org/branches/{branch.pk}/working-hours/",
            [],
            format="json",
        ).status_code
        == 403
    )
    assert (
        department_client.post(
            f"/api/v1/org/branches/{branch.pk}/holidays/",
            {"date": "2026-08-03", "name": "Department-only holiday"},
            format="json",
        ).status_code
        == 403
    )
    assert (
        department_client.post(
            "/api/v1/org/rooms/",
            {"branch": branch.pk, "name": "Shared room"},
            format="json",
        ).status_code
        == 403
    )


def test_org_mutations_reject_unknown_and_malformed_fields(as_role, tenant_a):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory, RoomFactory
    from apps.students.tests.factories import StudentProfileFactory

    client, _user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        department = DepartmentFactory(branch=source)
        room = RoomFactory(branch=source)
        student = StudentProfileFactory(branch=source)

    cases = (
        client.post(
            "/api/v1/org/branches/",
            {"name": "Bad", "slug": "bad", "admin": True},
            format="json",
        ),
        client.patch(
            f"/api/v1/org/departments/{department.pk}/",
            {"branch": target.pk},
            format="json",
        ),
        client.patch(
            f"/api/v1/org/rooms/{room.pk}/",
            {"branch": target.pk},
            format="json",
        ),
        client.post(
            "/api/v1/org/transfers/",
            {"student": student.pk, "to_branch": target.pk, "force": True},
            format="json",
        ),
    )
    assert all(response.status_code == 400 for response in cases)
    assert (
        client.post(
            "/api/v1/org/branches/",
            {"name": "Bad TZ", "slug": "bad-tz", "timezone": "Mars/Olympus"},
            format="json",
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/org/rooms/",
            {"branch": source.pk, "name": "Bad equipment", "equipment": [{"name": "TV"}]},
            format="json",
        ).status_code
        == 400
    )


def test_department_and_room_delete_are_soft(as_role, tenant_a):
    from apps.org.models import Department, Room
    from apps.org.tests.factories import BranchFactory, DepartmentFactory, RoomFactory

    client, _user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        room = RoomFactory(branch=branch)

    assert client.delete(f"/api/v1/org/departments/{department.pk}/").status_code == 204
    assert client.delete(f"/api/v1/org/rooms/{room.pk}/").status_code == 204
    with schema_context(tenant_a.schema_name):
        assert Department.objects.get(pk=department.pk).is_active is False
        assert Room.objects.get(pk=room.pk).is_active is False


def test_database_guards_structure_and_transfer_history(tenant_a):
    from apps.org.models import Branch, BranchTransfer, Department, Room
    from apps.org.services import transfer_student
    from apps.org.tests.factories import BranchFactory, DepartmentFactory, RoomFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        empty_branch = BranchFactory()
        department = DepartmentFactory()
        room = RoomFactory()
        source = BranchFactory()
        target = BranchFactory()
        student = StudentProfileFactory(branch=source)
        transfer = transfer_student(
            student_id=student.pk,
            to_branch_id=target.pk,
            allowed_branch_ids=None,
        )

        deletes = (
            lambda: Branch.objects.filter(pk=empty_branch.pk).delete(),
            lambda: Department.objects.filter(pk=department.pk).delete(),
            lambda: Room.objects.filter(pk=room.pk).delete(),
        )
        for delete in deletes:
            with pytest.raises(DatabaseError), transaction.atomic():
                delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            BranchTransfer.objects.filter(pk=transfer.pk).update(reason="rewritten")
        with pytest.raises(DatabaseError), transaction.atomic():
            BranchTransfer.objects.filter(pk=transfer.pk).delete()


def test_role_native_transfer_returns_stable_actor_and_student_ids(tenant_a, client_for):
    from apps.org.services import create_staff_account
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from core.session_auth import create_session

    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        student = StudentProfileFactory(
            branch=source,
            first_name="Ali",
            last_name="Karimov",
        )
        staff = create_staff_account(
            branch=source,
            role=Role.DIRECTOR,
            username="transfer-director",
            first_name="Amina",
            last_name="Director",
        )
        session = create_session(
            staff.user,
            principal_kind="staff",
            principal_id=staff.pk,
        )

    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
    response = client.post(
        "/api/v1/org/transfers/",
        {"student": student.pk, "to_branch": target.pk, "reason": "relocation"},
        format="json",
    )
    assert response.status_code == 201, response.content
    payload = response.json()["data"]
    assert payload["student"] == student.pk
    assert payload["student_public_id"] == student.student_id
    assert payload["student_name"] == "Ali Karimov"
    assert payload["actor_principal_kind"] == "staff"
    assert payload["actor_principal_id"] == staff.pk
    assert payload["actor_name"] == "Amina Director"
    assert "user" not in payload
    assert "actor" not in payload

    filtered = client.get("/api/v1/org/transfers/", {"student": student.pk})
    assert filtered.status_code == 200, filtered.content
    assert [row["id"] for row in filtered.json()["data"]] == [payload["id"]]


def test_department_only_grant_cannot_read_or_execute_branch_transfers(tenant_a, as_user):
    """Transfer history has no immutable department snapshot, so it must fail closed."""
    from apps.org.services import record_transfer
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        source_department = DepartmentFactory(branch=source)
        target_department = DepartmentFactory(branch=target)
        historical_student = StudentProfileFactory(branch=source)
        moving_student = StudentProfileFactory(branch=source)
        history = record_transfer(
            user=historical_student.user,
            from_branch=source,
            to_branch=target,
            reason="historical",
        )
        operator = _scoped_account(
            branch=source,
            department=source_department,
            permissions=("org:read", "org:write"),
        )
        account_type = operator.role_memberships.get().account_type
        RoleMembership.objects.create(
            user=operator,
            branch=target,
            department=target_department,
            role=Role.SUPPORT,
            account_type=account_type,
        )
        operator.refresh_from_db()
    client = as_user(tenant_a, operator)

    listed = client.get("/api/v1/org/transfers/")
    assert listed.status_code == 200
    assert listed.json()["data"] == []
    assert client.get(f"/api/v1/org/transfers/{history.pk}/").status_code == 404
    moved = client.post(
        "/api/v1/org/transfers/",
        {"student": moving_student.pk, "to_branch": target.pk},
        format="json",
    )
    assert moved.status_code == 404
    with schema_context(tenant_a.schema_name):
        moving_student.refresh_from_db()
        assert moving_student.branch_id == source.pk


def test_transfer_actor_and_student_snapshots_reject_forged_ownership(tenant_a):
    from django.db import IntegrityError

    from apps.org.models import BranchTransfer
    from apps.org.services import create_staff_account, record_transfer
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        student = StudentProfileFactory(branch=source)
        other_student = StudentProfileFactory(branch=source)
        actor = create_staff_account(
            branch=source,
            role=Role.SUPPORT,
            username="exact-transfer-actor",
        )
        other_actor = create_staff_account(
            branch=source,
            role=Role.SUPPORT,
            username="different-transfer-actor",
        )

        with pytest.raises(ValidationException), transaction.atomic():
            record_transfer(
                user=student.user,
                student=student,
                from_branch=source,
                to_branch=target,
                actor=actor.user,
                actor_principal_kind="staff",
                actor_principal_id=other_actor.pk,
            )
        with pytest.raises(ValidationException), transaction.atomic():
            record_transfer(
                user=student.user,
                student=student,
                from_branch=source,
                to_branch=target,
                actor=actor.user,
                actor_principal_kind="teacher",
                actor_principal_id=actor.pk,
            )

        # The database guard protects raw/bulk writers that bypass the service.
        with pytest.raises(IntegrityError), transaction.atomic():
            BranchTransfer.objects.create(
                user=student.user,
                student=student,
                student_public_id=student.student_id,
                student_name=student.get_full_name(),
                student_attribution_status=BranchTransfer.AttributionStatus.RESOLVED,
                from_branch=source,
                to_branch=target,
                actor=actor.user,
                actor_principal_kind="staff",
                actor_principal_id=other_actor.pk,
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            BranchTransfer.objects.create(
                user=student.user,
                student=other_student,
                student_public_id=other_student.student_id,
                student_name=other_student.get_full_name(),
                student_attribution_status=BranchTransfer.AttributionStatus.RESOLVED,
                from_branch=source,
                to_branch=target,
                actor=actor.user,
                actor_principal_kind="staff",
                actor_principal_id=actor.pk,
            )

        # Immutable display snapshots and branch direction are DB-enforced for
        # raw/bulk writers as well as the service path.
        student.branch = target
        student.save(update_fields=["branch", "updated_at"])
        with pytest.raises(IntegrityError), transaction.atomic():
            BranchTransfer.objects.create(
                user=student.user,
                student=student,
                student_public_id=student.student_id,
                student_name="Forged student display",
                student_attribution_status=BranchTransfer.AttributionStatus.RESOLVED,
                from_branch=source,
                to_branch=target,
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            BranchTransfer.objects.create(
                user=student.user,
                from_branch=source,
                to_branch=target,
                actor=actor.user,
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            BranchTransfer.objects.create(
                user=student.user,
                from_branch=source,
                to_branch=target,
                actor=actor.user,
                actor_principal_kind="staff",
                actor_principal_id=actor.pk,
                actor_name="Forged actor display",
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            BranchTransfer.objects.create(
                user=student.user,
                from_branch=source,
                to_branch=source,
            )
