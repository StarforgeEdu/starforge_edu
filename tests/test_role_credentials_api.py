"""Teacher and parent credential issuance stays on their role tables."""

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.parents.models import ParentProfile
from apps.teachers.models import TeacherProfile
from core.permissions import Role

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("kind", "create_url", "phone"),
    [
        ("teacher", "/api/v1/teachers/", "+998901113301"),
        ("parent", "/api/v1/parents/", "+998901113302"),
    ],
)
def test_role_credentials_endpoint_owns_password(kind, create_url, phone, tenant_a, as_role, client_for):
    director, _user = as_role(Role.DIRECTOR)
    body = {"phone": phone, "first_name": "Role", "last_name": "Account"}
    if kind == "teacher":
        with schema_context(tenant_a.schema_name):
            body["branch"] = BranchFactory().pk
    created = director.post(create_url, body, format="json")
    assert created.status_code == 201, created.content
    account_id = created.json()["data"]["id"]
    username = created.json()["data"]["username"]

    issued = director.post(f"{create_url}{account_id}/credentials/", {}, format="json")
    assert issued.status_code == 200, issued.content
    temporary = issued.json()["data"]["temporary_password"]
    login = client_for(tenant_a).post(
        "/api/v1/auth/role-login/",
        {"username": username, "password": temporary},
        format="json",
    )
    assert login.status_code == 200, login.content
    assert login.json()["data"]["role"] == kind

    model = TeacherProfile if kind == "teacher" else ParentProfile
    with schema_context(tenant_a.schema_name):
        account = model.objects.get(pk=account_id)
        assert account.check_password(temporary)
        assert not account.user.has_usable_password()
