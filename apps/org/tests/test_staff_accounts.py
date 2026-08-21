"""Staff accounts own identity and credentials across CRUD, login, and /me."""

import re

import pytest
from django.conf import settings
from django_tenants.utils import schema_context

from apps.access.models import AccountType
from apps.org.models import StaffProfile
from apps.org.tests.factories import BranchFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

STAFF_URL = "/api/v1/org/staff/"


def _staff_permission_user(tenant, *, user_in, branch, permissions):
    from apps.access.models import AccountTypePermission
    from apps.users.models import RoleMembership

    user = user_in(tenant)
    account_type = AccountType.objects.create(
        name=f"Staff API test role {user.pk}",
        slug=f"staff-api-test-role-{user.pk}",
        account_kind=AccountType.AccountKind.STAFF,
    )
    AccountTypePermission.objects.bulk_create(
        [
            AccountTypePermission(account_type=account_type, permission=permission)
            for permission in permissions
        ]
    )
    RoleMembership.objects.create(
        user=user,
        account_type=account_type,
        role=account_type.compatibility_role,
        branch=branch,
    )
    user.refresh_from_db()
    return user


def test_staff_directory_does_not_borrow_other_principal_or_hidden_branch_memberships(
    tenant_a,
    user_in,
    as_user,
):
    from apps.teachers.models import TeacherProfile
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        visible_branch = BranchFactory()
        hidden_branch = BranchFactory()
        target_user = user_in(tenant_a, roles=[Role.CASHIER], branch=hidden_branch)
        target = StaffProfile.objects.create(
            user=target_user,
            username="multi.profile.staff",
            first_name="Hidden",
        )
        TeacherProfile.objects.create(
            user=target_user,
            username="multi.profile.teacher",
            branch=visible_branch,
        )
        # This teacher assignment previously made the staff profile visible.
        RoleMembership.objects.create(
            user=target_user,
            role=Role.TEACHER,
            branch=visible_branch,
        )
        reader = _staff_permission_user(
            tenant_a,
            user_in=user_in,
            branch=visible_branch,
            permissions={"users:read"},
        )

    client = as_user(tenant_a, reader)
    hidden = client.get(STAFF_URL, {"page_size": 100})
    assert hidden.status_code == 200, hidden.content
    assert target.pk not in {row["id"] for row in hidden.json()["data"]}

    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.create(
            user=target_user,
            role=Role.CASHIER,
            branch=visible_branch,
        )
    visible = client.get(STAFF_URL, {"page_size": 100})
    row = next(item for item in visible.json()["data"] if item["id"] == target.pk)
    assert {membership["branch"] for membership in row["role_memberships"]} == {visible_branch.pk}
    assert "teacher" not in str(row["role_memberships"])
    assert hidden_branch.pk not in {membership["branch"] for membership in row["role_memberships"]}


def test_scoped_users_writer_cannot_mint_privileged_staff_account(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        owner_type = AccountType.objects.get(is_system=True, slug=Role.DIRECTOR)
        writer = _staff_permission_user(
            tenant_a,
            user_in=user_in,
            branch=branch,
            permissions={"users:write"},
        )

    response = as_user(tenant_a, writer).post(
        STAFF_URL,
        {
            "account_type": owner_type.pk,
            "branch": branch.pk,
            "phone": "+998901112299",
            "username": "forged.owner",
        },
        format="json",
    )

    assert response.status_code == 403
    with schema_context(tenant_a.schema_name):
        assert not StaffProfile.objects.filter(username="forged.owner").exists()


def test_one_branch_writer_cannot_take_over_multi_branch_staff_identity(
    tenant_a,
    user_in,
    as_user,
):
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        visible_branch = BranchFactory()
        hidden_branch = BranchFactory()
        target_user = user_in(tenant_a, roles=[Role.CASHIER], branch=visible_branch)
        RoleMembership.objects.create(
            user=target_user,
            role=Role.CASHIER,
            branch=hidden_branch,
        )
        target = StaffProfile.objects.create(
            user=target_user,
            username="protected.multi.branch",
            first_name="Original",
        )
        writer = _staff_permission_user(
            tenant_a,
            user_in=user_in,
            branch=visible_branch,
            permissions={"users:read", "users:write"},
        )

    client = as_user(tenant_a, writer)
    update = client.patch(
        f"{STAFF_URL}{target.pk}/",
        {"first_name": "Taken over"},
        format="json",
    )
    credentials = client.post(
        f"{STAFF_URL}{target.pk}/credentials/",
        {},
        format="json",
    )
    delete = client.delete(f"{STAFF_URL}{target.pk}/")

    assert update.status_code == credentials.status_code == delete.status_code == 404
    with schema_context(tenant_a.schema_name):
        target.refresh_from_db()
        assert target.first_name == "Original"
        assert target.is_active is True


def test_staff_account_api_and_role_owned_credentials(tenant_a, as_role, client_for):
    director, _user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cashier_type = AccountType.objects.get(is_system=True, slug=Role.CASHIER)

    response = director.post(
        "/api/v1/org/staff/",
        {
            "username": "casey.cashier",
            "phone": "+998901112233",
            "first_name": "Casey",
            "last_name": "Cashier",
            "account_type": cashier_type.pk,
            "branch": branch.pk,
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    payload = response.json()["data"]
    assert payload["username"] == "casey.cashier"
    assert "user" not in payload
    assert payload["role_memberships"][0]["account_type_slug"] == Role.CASHIER
    staff_id = payload["id"]

    credentials = director.post(f"/api/v1/org/staff/{staff_id}/credentials/", {}, format="json")
    assert credentials.status_code == 200, credentials.content
    temporary = credentials.json()["data"]["temporary_password"]

    login = client_for(tenant_a).post(
        "/api/v1/auth/role-login/",
        {"username": "casey.cashier", "password": temporary},
        format="json",
    )
    assert login.status_code == 200, login.content
    access = login.json()["data"]["access"]
    staff_client = client_for(tenant_a)
    staff_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    me = staff_client.get("/api/v1/users/me/")
    assert me.status_code == 200
    assert me.json()["data"]["principal_kind"] == "staff"
    assert me.json()["data"]["id"] == staff_id

    changed = staff_client.post(
        "/api/v1/auth/password/change/",
        {"old_password": temporary, "new_password": "Comet-Compass-84"},
        format="json",
    )
    assert changed.status_code == 200, changed.content
    changed_access = changed.json()["data"]["access"]
    changed_client = client_for(tenant_a)
    changed_client.credentials(HTTP_AUTHORIZATION=f"Bearer {changed_access}")
    assert changed_client.get("/api/v1/users/me/").json()["data"]["principal_kind"] == "staff"

    assert (
        client_for(tenant_a)
        .post(
            "/api/v1/auth/role-login/",
            {"username": "casey.cashier", "password": temporary},
            format="json",
        )
        .status_code
        == 401
    )
    assert (
        client_for(tenant_a)
        .post(
            "/api/v1/auth/role-login/",
            {"username": "casey.cashier", "password": "Comet-Compass-84"},
            format="json",
        )
        .status_code
        == 200
    )

    with schema_context(tenant_a.schema_name):
        staff = StaffProfile.objects.get(pk=staff_id)
        assert staff.check_password("Comet-Compass-84")
        assert not staff.user.has_usable_password()


def test_staff_role_password_reset_uses_role_contact(tenant_a, as_role, client_for, sms_outbox):
    director, _user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    created = director.post(
        "/api/v1/org/staff/",
        {
            "username": "reset.accountant",
            "phone": "+998901112244",
            "role": Role.ACCOUNTANT,
            "branch": branch.pk,
        },
        format="json",
    )
    assert created.status_code == 201
    staff_id = created.json()["data"]["id"]
    credentials = director.post(f"/api/v1/org/staff/{staff_id}/credentials/", {}, format="json")
    old_password = credentials.json()["data"]["temporary_password"]

    anonymous = client_for(tenant_a)
    requested = anonymous.post(
        "/api/v1/auth/password/reset/request/",
        {"identifier": "+998901112244", "account_type": "staff"},
        format="json",
    )
    assert requested.status_code == 202
    match = re.search(rf"\b(\d{{{settings.OTP_LENGTH}}})\b", sms_outbox[-1]["text"])
    assert match
    confirmed = anonymous.post(
        "/api/v1/auth/password/reset/confirm/",
        {
            "identifier": "+998901112244",
            "account_type": "staff",
            "code": match.group(1),
            "new_password": "Reset-Orbit-93",
        },
        format="json",
    )
    assert confirmed.status_code == 204, confirmed.content
    assert (
        anonymous.post(
            "/api/v1/auth/role-login/",
            {"username": "reset.accountant", "password": old_password},
            format="json",
        ).status_code
        == 401
    )
    assert (
        anonymous.post(
            "/api/v1/auth/role-login/",
            {"username": "reset.accountant", "password": "Reset-Orbit-93"},
            format="json",
        ).status_code
        == 200
    )
