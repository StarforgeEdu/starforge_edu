"""Row-level scoping for parent/student self-service reads (TD-5).

The permission matrix grants PARENT/STUDENT the read codes; these tests pin
that the selectors then narrow the rows to own children / own profile only
(the matrix test asserts status codes; this one asserts row sets).
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.parents.models import Guardian, ParentProfile, PickupAuthorization
from apps.students.tests.factories import StudentProfileFactory
from core.historical_scope import ScopeAttributionStatus
from core.permissions import Role

pytestmark = pytest.mark.django_db


@pytest.fixture
def family(tenant_a, user_in):
    """Two students with different parents on one branch; returns the actors."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        own_child = StudentProfileFactory.create(branch=branch)
        other_child = StudentProfileFactory.create(branch=branch)
    parent_user = user_in(tenant_a, roles=[Role.PARENT], branch=branch)
    other_parent_user = user_in(tenant_a, roles=[Role.PARENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        parent = ParentProfile.objects.create(user=parent_user)
        other_parent = ParentProfile.objects.create(user=other_parent_user)
        Guardian.objects.create(parent=parent, student=own_child, relationship="mother", is_primary=True)
        Guardian.objects.create(
            parent=other_parent, student=other_child, relationship="father", is_primary=True
        )
    return {
        "branch": branch,
        "own_child": own_child,
        "other_child": other_child,
        "parent_user": parent_user,
        "parent": parent,
        "other_parent": other_parent,
    }


def test_parent_sees_only_own_children(tenant_a, as_user, family):
    client = as_user(tenant_a, family["parent_user"])

    body = client.get("/api/v1/students/").json()
    assert [s["id"] for s in body["data"]] == [family["own_child"].id]

    resp = client.get(f"/api/v1/parents/{family['parent'].id}/students/")
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()["data"]] == [family["own_child"].id]


def test_parent_list_returns_only_self(tenant_a, as_user, family):
    client = as_user(tenant_a, family["parent_user"])
    body = client.get("/api/v1/parents/").json()
    assert [p["id"] for p in body["data"]] == [family["parent"].id]


def test_parent_cannot_reach_other_parents_profile(tenant_a, as_user, family):
    client = as_user(tenant_a, family["parent_user"])
    # Out of the scoped queryset -> 404, not 403 (no existence leak).
    assert client.get(f"/api/v1/parents/{family['other_parent'].id}/students/").status_code == 404


def test_parent_pickups_scoped_to_own_children(tenant_a, as_user, family):
    with schema_context(tenant_a.schema_name):
        own_pickup = PickupAuthorization.objects.create(
            student=family["own_child"], full_name="Granny", phone="+998905558801"
        )
        PickupAuthorization.objects.create(
            student=family["other_child"], full_name="Stranger", phone="+998905558802"
        )
    client = as_user(tenant_a, family["parent_user"])
    body = client.get("/api/v1/parents/pickups/").json()
    assert [p["id"] for p in body["data"]] == [own_pickup.id]


def test_student_sees_only_self(tenant_a, user_in, as_user, family):
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=family["branch"])
    with schema_context(tenant_a.schema_name):
        own_profile = StudentProfileFactory.create(user=student_user, branch=family["branch"])
    client = as_user(tenant_a, student_user)

    body = client.get("/api/v1/students/").json()
    assert [s["id"] for s in body["data"]] == [own_profile.id]


def test_registrar_parent_surfaces_and_write_targets_are_department_scoped(tenant_a, user_in, as_user):
    """List/detail joins and guardian/pickup FK ids must share one exact scope."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import DepartmentFactory
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.users.models import RoleMembership

    registrar = user_in(tenant_a)
    branch_registrar = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        other_branch = BranchFactory()
        own_department = DepartmentFactory(branch=branch)
        sibling_department = DepartmentFactory(branch=branch)
        foreign_department = DepartmentFactory(branch=other_branch)
        own_cohort = CohortFactory(branch=branch, department=own_department)
        sibling_cohort = CohortFactory(branch=branch, department=sibling_department)
        foreign_cohort = CohortFactory(branch=other_branch, department=foreign_department)

        own_student = StudentProfileFactory(branch=branch, current_cohort=own_cohort)
        own_student_2 = StudentProfileFactory(branch=branch, current_cohort=own_cohort)
        sibling_student = StudentProfileFactory(branch=branch, current_cohort=sibling_cohort)
        foreign_student = StudentProfileFactory(
            branch=other_branch,
            current_cohort=foreign_cohort,
        )

        own_parent = ParentProfileFactory()
        sibling_parent = ParentProfileFactory()
        foreign_parent = ParentProfileFactory()
        shared_parent = ParentProfileFactory(workplace="Before")
        own_guardian = GuardianFactory(parent=own_parent, student=own_student)
        GuardianFactory(parent=sibling_parent, student=sibling_student)
        GuardianFactory(parent=foreign_parent, student=foreign_student)
        shared_own_guardian = GuardianFactory(parent=shared_parent, student=own_student_2)
        GuardianFactory(parent=shared_parent, student=sibling_student)

        own_pickup = PickupAuthorization.objects.create(
            student=own_student,
            full_name="Own pickup",
            phone="+998905559001",
        )
        sibling_pickup = PickupAuthorization.objects.create(
            student=sibling_student,
            full_name="Sibling pickup",
            phone="+998905559002",
        )
        foreign_pickup = PickupAuthorization.objects.create(
            student=foreign_student,
            full_name="Foreign pickup",
            phone="+998905559003",
        )
        orphan_for_own = ParentProfileFactory(
            branch_at_creation=branch,
            department_at_creation=own_department,
            attribution_status=ScopeAttributionStatus.CAPTURED,
        )
        orphan_for_foreign = ParentProfileFactory(
            branch_at_creation=other_branch,
            department_at_creation=foreign_department,
            attribution_status=ScopeAttributionStatus.CAPTURED,
        )

        RoleMembership.objects.create(
            user=registrar,
            branch=branch,
            department=own_department,
            role=Role.REGISTRAR,
        )
        RoleMembership.objects.create(
            user=branch_registrar,
            branch=branch,
            role=Role.REGISTRAR,
        )
        registrar.refresh_from_db()
        branch_registrar.refresh_from_db()

    client = as_user(tenant_a, registrar)
    parent_ids = {row["id"] for row in client.get("/api/v1/parents/").json()["data"]}
    assert parent_ids == {own_parent.id, shared_parent.id, orphan_for_own.id}
    assert client.get(f"/api/v1/parents/{sibling_parent.id}/").status_code == 404

    guardian_ids = {row["id"] for row in client.get("/api/v1/parents/guardians/").json()["data"]}
    assert guardian_ids == {own_guardian.id, shared_own_guardian.id}
    pickup_ids = {row["id"] for row in client.get("/api/v1/parents/pickups/").json()["data"]}
    assert pickup_ids == {own_pickup.id}
    assert client.get(f"/api/v1/parents/pickups/{sibling_pickup.id}/").status_code == 404
    assert client.get(f"/api/v1/parents/pickups/{foreign_pickup.id}/").status_code == 404

    shared_children = client.get(f"/api/v1/parents/{shared_parent.id}/students/")
    assert shared_children.status_code == 200
    assert {row["id"] for row in shared_children.json()["data"]} == {own_student_2.id}
    shared_update = client.patch(
        f"/api/v1/parents/{shared_parent.id}/",
        {"workplace": "Cross-scope mutation"},
        format="json",
    )
    assert shared_update.status_code == 404

    allowed_guardian = client.post(
        "/api/v1/parents/guardians/",
        {
            "parent": orphan_for_own.id,
            "student": own_student.id,
            "relationship": "other",
        },
        format="json",
    )
    assert allowed_guardian.status_code == 201, allowed_guardian.content
    denied_guardian = client.post(
        "/api/v1/parents/guardians/",
        {
            "parent": orphan_for_foreign.id,
            "student": foreign_student.id,
            "relationship": "other",
        },
        format="json",
    )
    assert denied_guardian.status_code == 400
    assert denied_guardian.json()["code"] == "invalid_parent"

    denied_pickup = client.post(
        "/api/v1/parents/pickups/",
        {
            "student": foreign_student.id,
            "full_name": "Denied",
            "phone": "+998905559004",
        },
        format="json",
    )
    assert denied_pickup.status_code == 400
    assert denied_pickup.json()["code"] == "invalid_student"
    denied_move = client.patch(
        f"/api/v1/parents/pickups/{own_pickup.id}/",
        {"student": sibling_student.id},
        format="json",
    )
    assert denied_move.status_code == 400
    assert denied_move.json()["code"] == "validation_error"
    assert set(denied_move.json()["errors"]) == {"student"}

    branch_client = as_user(tenant_a, branch_registrar)
    branch_parent_ids = {row["id"] for row in branch_client.get("/api/v1/parents/").json()["data"]}
    assert {own_parent.id, sibling_parent.id, shared_parent.id} <= branch_parent_ids
    assert foreign_parent.id not in branch_parent_ids


def test_parent_grant_cannot_borrow_an_unrelated_membership_scope(tenant_a, user_in, as_user):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        local = BranchFactory(name="Parent grant branch", slug="parent-grant-branch")
        remote = BranchFactory(name="Parent unrelated branch", slug="parent-unrelated-branch")
        local_student = StudentProfileFactory(branch=local)
        remote_student = StudentProfileFactory(branch=remote)
        local_parent = ParentProfileFactory()
        remote_parent = ParentProfileFactory()
        GuardianFactory(parent=local_parent, student=local_student)
        GuardianFactory(parent=remote_parent, student=remote_student)

        granting_type = AccountType.objects.create(
            name="Scoped parent operator",
            slug="scoped-parent-operator",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.bulk_create(
            [
                AccountTypePermission(account_type=granting_type, permission="parents:read"),
                AccountTypePermission(account_type=granting_type, permission="parents:write"),
            ]
        )
        unrelated_type = AccountType.objects.create(
            name="Unrelated parent membership",
            slug="unrelated-parent-membership",
            account_kind=AccountType.AccountKind.STAFF,
        )
        actor = user_in(tenant_a)
        RoleMembership.objects.create(
            user=actor,
            branch=local,
            account_type=granting_type,
            role=granting_type.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=remote,
            account_type=unrelated_type,
            role=unrelated_type.compatibility_role,
        )
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    listing = client.get("/api/v1/parents/")
    assert listing.status_code == 200, listing.content
    assert {row["id"] for row in listing.json()["data"]} == {local_parent.pk}
    assert client.get(f"/api/v1/parents/{remote_parent.pk}/").status_code == 404
    denied = client.post(
        "/api/v1/parents/pickups/",
        {
            "student": remote_student.pk,
            "full_name": "Denied",
            "phone": "+998905559099",
        },
        format="json",
    )
    assert denied.status_code == 400
    assert denied.json()["code"] == "invalid_student"


def test_family_directory_grant_does_not_reveal_or_write_custody_notes(tenant_a, user_in, as_user):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Family directory branch", slug="family-directory-branch")
        student = StudentProfileFactory(branch=branch)
        second_student = StudentProfileFactory(branch=branch)
        parent = ParentProfileFactory()
        second_parent = ParentProfileFactory()
        guardian = GuardianFactory(
            parent=parent,
            student=student,
            custody_notes="Restricted court-order details",
        )
        directory_type = AccountType.objects.create(
            name="Family directory operator",
            slug="family-directory-operator",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.bulk_create(
            [
                AccountTypePermission(account_type=directory_type, permission="parents:read"),
                AccountTypePermission(account_type=directory_type, permission="parents:write"),
            ]
        )
        actor = user_in(tenant_a)
        RoleMembership.objects.create(
            user=actor,
            branch=branch,
            account_type=directory_type,
            role=directory_type.compatibility_role,
        )
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    detail = client.get(f"/api/v1/parents/guardians/{guardian.pk}/")
    assert detail.status_code == 200
    assert detail.json()["data"]["custody_notes"] is None

    denied = client.post(
        "/api/v1/parents/guardians/",
        {
            "parent": second_parent.pk,
            "student": second_student.pk,
            "relationship": "guardian",
            "custody_notes": "Attempted restricted note",
        },
        format="json",
    )
    assert denied.status_code == 403
