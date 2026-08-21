"""Authoritative permission/scope bootstrap regressions for ``GET /users/me/``."""

from __future__ import annotations

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.access.models import AccountType, AccountTypePermission
from core.permissions import Role

pytestmark = pytest.mark.django_db

ME = "/api/v1/users/me/"


def _custom_type(*, name: str, slug: str, permissions: set[str]) -> AccountType:
    account_type = AccountType.objects.create(
        name=name,
        slug=slug,
        account_kind=AccountType.AccountKind.STAFF,
    )
    AccountTypePermission.objects.bulk_create(
        [
            AccountTypePermission(account_type=account_type, permission=permission)
            for permission in sorted(permissions)
        ]
    )
    return account_type


def test_me_reports_director_master_grant_and_named_scope(tenant_a, as_role):
    from apps.org.models import CenterSettings

    client, user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        membership = user.role_memberships.select_related("branch").get()
        settings = CenterSettings.load()
        settings.default_language = "ru"
        settings.currency_primary = "EUR"
        settings.save(update_fields=("default_language", "currency_primary"))

    response = client.get(ME)

    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["effective_permissions"] == ["*:*"]
    assert data["organization_locale"] == "ru"
    assert data["primary_currency"] == "EUR"
    assert data["session_id"]
    assert data["session_created_at"]
    assert data["session_last_activity_at"]
    assert data["session_expires_at"]
    assert data["session_idle_expires_at"]
    assert data["server_time"]
    assert data["scopes"] == [
        {
            "branch": None,
            "department": None,
            "effective_permissions": ["*:*"],
        }
    ]
    assert data["role_memberships"][0]["branch_name"] == membership.branch.name


def test_me_unions_custom_membership_grants_without_borrowing_scope(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        local_branch = BranchFactory(name="Central Campus")
        local_department = DepartmentFactory(branch=local_branch, name="Languages")
        remote_branch = BranchFactory(name="East Campus")
        user = user_in(tenant_a)
        learners = _custom_type(
            name="Learner Viewer",
            slug="learner-viewer",
            permissions={"students:read", "cohorts:read"},
        )
        finance = _custom_type(
            name="Finance Viewer",
            slug="finance-viewer",
            permissions={"finance:read"},
        )
        RoleMembership.objects.create(
            user=user,
            branch=local_branch,
            department=local_department,
            account_type=learners,
            role=learners.compatibility_role,
        )
        RoleMembership.objects.create(
            user=user,
            branch=remote_branch,
            account_type=finance,
            role=finance.compatibility_role,
        )
        user.refresh_from_db()

    response = as_user(tenant_a, user).get(ME)

    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["effective_permissions"] == ["cohorts:read", "finance:read", "students:read"]
    assert data["scopes"] == [
        {
            "branch": {"id": local_branch.pk, "name": "Central Campus"},
            "department": {"id": local_department.pk, "name": "Languages"},
            "effective_permissions": ["cohorts:read", "students:read"],
        },
        {
            "branch": {"id": remote_branch.pk, "name": "East Campus"},
            "department": None,
            "effective_permissions": ["finance:read"],
        },
    ]


def test_me_legacy_override_carves_wildcard_without_overreporting(
    tenant_a,
    as_role,
    as_user,
):
    from apps.access.services import set_override

    _client, user = as_role(Role.HEAD_OF_DEPT)
    with schema_context(tenant_a.schema_name):
        membership = user.role_memberships.get()
        # Deliberately preserve one pre-account-type compatibility assignment.
        user.role_memberships.filter(pk=membership.pk).update(account_type_id=None)
        set_override(
            role=Role.HEAD_OF_DEPT,
            permission="students:write",
            effect="revoke",
        )
        set_override(
            role=Role.HEAD_OF_DEPT,
            permission="finance:read",
            effect="grant",
        )
        user.refresh_from_db()

    response = as_user(tenant_a, user).get(ME)

    assert response.status_code == 200, response.content
    data = response.json()["data"]
    permissions = data["effective_permissions"]
    assert permissions == sorted(permissions)
    assert "students:read" in permissions
    assert "finance:read" in permissions
    assert "students:write" not in permissions
    assert "students:*" not in permissions
    assert data["scopes"][0]["effective_permissions"] == permissions
    assert data["role_memberships"][0]["legacy_role"] == Role.HEAD_OF_DEPT


def test_me_excludes_revoked_memberships_and_inactive_account_types(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Inactive Scope")
        user = user_in(tenant_a)
        revoked_type = _custom_type(
            name="Revoked Students",
            slug="revoked-students",
            permissions={"students:read"},
        )
        inactive_type = _custom_type(
            name="Inactive Finance",
            slug="inactive-finance",
            permissions={"finance:read"},
        )
        inactive_type.is_active = False
        inactive_type.save(update_fields=("is_active",))
        revoked = RoleMembership.objects.create(
            user=user,
            branch=branch,
            account_type=revoked_type,
            role=revoked_type.compatibility_role,
        )
        RoleMembership.objects.filter(pk=revoked.pk).update(revoked_at=timezone.now())
        RoleMembership.objects.create(
            user=user,
            branch=branch,
            account_type=inactive_type,
            role=inactive_type.compatibility_role,
        )
        user.refresh_from_db()

    response = as_user(tenant_a, user).get(ME)

    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["effective_permissions"] == []
    assert data["scopes"] == []
    assert data["role_memberships"] == []


def test_me_permission_bootstrap_has_no_membership_n_plus_one(
    tenant_a,
    user_in,
    as_user,
    django_assert_max_num_queries,
):
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        user = user_in(tenant_a)
        for index in range(6):
            branch = BranchFactory(name=f"Performance Scope {index}")
            account_type = _custom_type(
                name=f"Scoped Viewer {index}",
                slug=f"scoped-viewer-{index}",
                permissions={"students:read"},
            )
            RoleMembership.objects.create(
                user=user,
                branch=branch,
                account_type=account_type,
                role=account_type.compatibility_role,
            )
        user.refresh_from_db()
    client = as_user(tenant_a, user)

    with django_assert_max_num_queries(9):
        response = client.get(ME)

    assert response.status_code == 200, response.content
    assert len(response.json()["data"]["scopes"]) == 6
