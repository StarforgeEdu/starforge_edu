"""Authorization and privacy regressions for the management user directory."""

from __future__ import annotations

from datetime import date

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.access.models import AccountType, AccountTypePermission
from apps.org.tests.factories import BranchFactory, DepartmentFactory
from apps.users.models import RoleMembership
from apps.users.tests.factories import UserFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

DIRECTORY = "/api/v1/users/"


def _account_type(
    slug: str,
    *,
    grants: set[str] | None = None,
    active: bool = True,
) -> AccountType:
    account_type = AccountType.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        account_kind=AccountType.AccountKind.STAFF,
        is_active=active,
    )
    AccountTypePermission.objects.bulk_create(
        [
            AccountTypePermission(account_type=account_type, permission=permission)
            for permission in sorted(grants or set())
        ]
    )
    return account_type


def _assign(
    user,
    *,
    branch,
    account_type: AccountType,
    department=None,
    revoked: bool = False,
) -> RoleMembership:
    membership = RoleMembership.objects.create(
        user=user,
        branch=branch,
        department=department,
        account_type=account_type,
        role=account_type.compatibility_role,
    )
    if revoked:
        RoleMembership.objects.filter(pk=membership.pk).update(revoked_at=timezone.now())
        membership.refresh_from_db()
    user.refresh_from_db()
    return membership


def _ids(response) -> set[int]:
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["data"]}


def test_branch_grant_sees_union_of_active_users_only_and_detail_is_404_outside_scope(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        local = BranchFactory(name="Local", slug="local-users")
        second_local = BranchFactory(name="Second Local", slug="second-local-users")
        remote = BranchFactory(name="Remote", slug="remote-users")
        reader_type = _account_type("branch-user-reader", grants={"users:read"})
        target_type = _account_type("ordinary-directory-target")

        viewer = user_in(tenant_a)
        _assign(viewer, branch=local, account_type=reader_type)
        _assign(viewer, branch=second_local, account_type=reader_type)
        local_target = UserFactory(username="local-target")
        _assign(local_target, branch=local, account_type=target_type)
        second_local_target = UserFactory(username="second-local-target")
        _assign(second_local_target, branch=second_local, account_type=target_type)
        remote_target = UserFactory(username="remote-target")
        _assign(remote_target, branch=remote, account_type=target_type)

    client = as_user(tenant_a, viewer)

    assert local_target.pk in _ids(client.get(DIRECTORY))
    assert second_local_target.pk in _ids(client.get(DIRECTORY))
    assert remote_target.pk not in _ids(client.get(DIRECTORY))
    assert client.get(f"{DIRECTORY}{local_target.pk}/").status_code == 200
    hidden = client.get(f"{DIRECTORY}{remote_target.pk}/")
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "not_found"


def test_department_grant_does_not_expand_to_branch_or_disclose_remote_memberships(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        local = BranchFactory(name="Department Campus", slug="department-campus")
        remote = BranchFactory(name="Remote Campus", slug="remote-campus-directory")
        languages = DepartmentFactory(branch=local, name="Languages", slug="languages-directory")
        science = DepartmentFactory(branch=local, name="Science", slug="science-directory")
        reader_type = _account_type("department-user-reader", grants={"users:read"})
        target_type = _account_type("department-directory-target")

        viewer = user_in(tenant_a)
        _assign(viewer, branch=local, department=languages, account_type=reader_type)
        visible = UserFactory(username="language-user")
        visible_membership = _assign(
            visible,
            branch=local,
            department=languages,
            account_type=target_type,
        )
        remote_membership = _assign(visible, branch=remote, account_type=target_type)
        other_department = UserFactory(username="science-user")
        _assign(other_department, branch=local, department=science, account_type=target_type)
        branch_wide = UserFactory(username="branch-wide-user")
        _assign(branch_wide, branch=local, account_type=target_type)

    client = as_user(tenant_a, viewer)
    visible_ids = _ids(client.get(DIRECTORY))

    assert visible.pk in visible_ids
    assert other_department.pk not in visible_ids
    assert branch_wide.pk not in visible_ids
    detail = client.get(f"{DIRECTORY}{visible.pk}/")
    assert detail.status_code == 200, detail.content
    membership_ids = {membership["id"] for membership in detail.json()["data"]["role_memberships"]}
    assert membership_ids == {visible_membership.pk}
    assert remote_membership.pk not in membership_ids


def test_revoked_and_inactive_target_memberships_do_not_create_visibility(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Active Assignments", slug="active-assignments-directory")
        reader_type = _account_type("active-user-reader", grants={"users:read"})
        active_target_type = _account_type("active-directory-target")
        inactive_target_type = _account_type("inactive-directory-target", active=False)
        viewer = user_in(tenant_a)
        _assign(viewer, branch=branch, account_type=reader_type)

        visible = UserFactory(username="active-target")
        _assign(visible, branch=branch, account_type=active_target_type)
        revoked = UserFactory(username="revoked-target")
        _assign(revoked, branch=branch, account_type=active_target_type, revoked=True)
        inactive = UserFactory(username="inactive-target")
        _assign(inactive, branch=branch, account_type=inactive_target_type)

    visible_ids = _ids(as_user(tenant_a, viewer).get(DIRECTORY))
    assert visible.pk in visible_ids
    assert revoked.pk not in visible_ids
    assert inactive.pk not in visible_ids


@pytest.mark.parametrize("assignment_state", ["revoked", "inactive"])
def test_revoked_or_inactive_reader_assignment_grants_no_directory_access(
    tenant_a,
    user_in,
    as_user,
    assignment_state,
):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(
            name=f"{assignment_state.title()} Reader",
            slug=f"{assignment_state}-reader-directory",
        )
        reader_type = _account_type(
            f"{assignment_state}-user-reader",
            grants={"users:read"},
            active=assignment_state != "inactive",
        )
        viewer = user_in(tenant_a)
        _assign(
            viewer,
            branch=branch,
            account_type=reader_type,
            revoked=assignment_state == "revoked",
        )

    response = as_user(tenant_a, viewer).get(DIRECTORY)
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_director_is_organization_wide_including_unassigned_users(tenant_a, as_role):
    client, _director = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        unassigned = UserFactory(username="unassigned-organization-user")

    assert unassigned.pk in _ids(client.get(DIRECTORY, {"page_size": 100}))
    assert client.get(f"{DIRECTORY}{unassigned.pk}/").status_code == 200


def test_unrelated_director_role_cannot_lend_global_scope_to_a_local_grant(
    tenant_a,
    user_in,
    as_user,
):
    """The compatibility role is not itself a permission-bearing boundary.

    This deliberately models a stale/malformed migrated assignment: its stored
    legacy role says director while its canonical AccountType grants nothing.
    A separate AccountType grants users:read in one branch.  The stale role must
    not turn that local grant into tenant-wide directory access.
    """
    with schema_context(tenant_a.schema_name):
        local = BranchFactory(name="Permission Local", slug="permission-local")
        remote = BranchFactory(name="Permission Remote", slug="permission-remote")
        reader_type = _account_type("local-directory-reader", grants={"users:read"})
        unrelated_type = _account_type("unrelated-empty-director")
        target_type = _account_type("permission-scope-target")
        viewer = user_in(tenant_a)
        _assign(viewer, branch=local, account_type=reader_type)
        unrelated = _assign(viewer, branch=remote, account_type=unrelated_type)
        RoleMembership.objects.filter(pk=unrelated.pk).update(role=Role.DIRECTOR)

        local_target = UserFactory(username="permission-local-target")
        _assign(local_target, branch=local, account_type=target_type)
        remote_target = UserFactory(username="permission-remote-target")
        _assign(remote_target, branch=remote, account_type=target_type)
        unassigned = UserFactory(username="permission-unassigned-target")
        viewer.refresh_from_db()

    client = as_user(tenant_a, viewer)
    visible_ids = _ids(client.get(DIRECTORY, {"page_size": 100}))

    assert local_target.pk in visible_ids
    assert remote_target.pk not in visible_ids
    assert unassigned.pk not in visible_ids
    assert client.get(f"{DIRECTORY}{remote_target.pk}/").status_code == 404

    bootstrap = client.get("/api/v1/users/me/")
    assert bootstrap.status_code == 200, bootstrap.content
    assert bootstrap.json()["data"]["scopes"] == [
        {
            "branch": {"id": local.pk, "name": "Permission Local"},
            "department": None,
            "effective_permissions": ["users:read"],
        }
    ]


def test_global_superuser_authority_is_organization_wide(tenant_a, as_user):
    with schema_context(tenant_a.schema_name):
        global_user = UserFactory(username="global-directory-authority", is_superuser=True)
        unassigned = UserFactory(username="global-visible-unassigned")

    client = as_user(tenant_a, global_user)
    assert unassigned.pk in _ids(client.get(DIRECTORY, {"page_size": 100}))
    assert client.get(f"{DIRECTORY}{unassigned.pk}/").status_code == 200


def test_list_is_pii_minimized_and_search_uses_only_approved_business_identifiers(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Lookup", slug="lookup-directory")
        reader_type = _account_type("lookup-user-reader", grants={"users:read"})
        target_type = _account_type("lookup-directory-target")
        viewer = user_in(tenant_a)
        _assign(viewer, branch=branch, account_type=reader_type)
        target = UserFactory(
            username="approved.lookup",
            first_name="Needle",
            last_name="Person",
            phone="+998901234567",
            email="private.directory@example.com",
            birthdate=date(1990, 1, 2),
            gender="f",
        )
        _assign(target, branch=branch, account_type=target_type)

    client = as_user(tenant_a, viewer)
    rows = client.get(DIRECTORY, {"search": "Needle"}).json()["data"]
    row = next(item for item in rows if item["id"] == target.pk)
    assert set(row) == {"id", "username", "full_name", "phone", "is_active", "last_seen_at"}
    assert target.pk in _ids(client.get(DIRECTORY, {"search": "+998901234567"}))
    assert target.pk not in _ids(client.get(DIRECTORY, {"search": "private.directory@example.com"}))

    detail = client.get(f"{DIRECTORY}{target.pk}/").json()["data"]
    assert detail["email"] == "private.directory@example.com"
    assert detail["birthdate"] == "1990-01-02"


def test_directory_prefetch_has_no_per_user_query_growth(
    tenant_a,
    user_in,
    as_user,
    django_assert_max_num_queries,
):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Query Budget", slug="query-budget-directory")
        reader_type = _account_type("query-budget-reader", grants={"users:read"})
        target_type = _account_type("query-budget-target")
        viewer = user_in(tenant_a)
        _assign(viewer, branch=branch, account_type=reader_type)
        for index in range(12):
            target = UserFactory(username=f"query-target-{index}")
            _assign(target, branch=branch, account_type=target_type)

    client = as_user(tenant_a, viewer)
    with django_assert_max_num_queries(9):
        response = client.get(DIRECTORY, {"page_size": 100})
    assert response.status_code == 200, response.content
    assert len(response.json()["data"]) == 13
