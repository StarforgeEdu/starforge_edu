"""Staff accounts own identity and credentials across CRUD, login, and /me."""

import re

import pytest
from django.conf import settings
from django_tenants.utils import schema_context

from apps.org.models import StaffProfile
from apps.org.tests.factories import BranchFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_staff_account_api_and_role_owned_credentials(tenant_a, as_role, client_for):
    director, _user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()

    response = director.post(
        "/api/v1/org/staff/",
        {
            "username": "casey.cashier",
            "phone": "+998901112233",
            "first_name": "Casey",
            "last_name": "Cashier",
            "role": Role.CASHIER,
            "branch": branch.pk,
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    payload = response.json()["data"]
    assert payload["username"] == "casey.cashier"
    assert "user" not in payload
    assert payload["role_memberships"][0]["role"] == Role.CASHIER
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
    assert me.json()["data"]["account_type"] == "staff"
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
    assert changed_client.get("/api/v1/users/me/").json()["data"]["account_type"] == "staff"

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
