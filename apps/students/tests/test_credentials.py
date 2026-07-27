"""Staff-issued one-time student login credentials (owner: "give a one-time password
and the app forces a change on first login"). Student accounts are created passwordless;
staff issue a temp password via the credentials endpoint (set on the linked User — the
source of truth /role-login/ checks), the account is flagged must-change, and the temp is
returned once for hand-off."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.students.models import StudentProfile
from apps.students.services import create_student
from core.permissions import Role

pytestmark = pytest.mark.django_db


def _registrar_and_student(tenant, user_in, as_user):
    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        sid = create_student(branch=branch, phone="+998905559001").id
    client = as_user(tenant, user_in(tenant, roles=[Role.REGISTRAR], branch=branch))
    return branch, client, sid


def test_staff_issues_one_time_credentials_and_student_can_authenticate(tenant_a, user_in, as_user):
    _, client, sid = _registrar_and_student(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        sp = StudentProfile.objects.get(pk=sid)
        assert sp.user.has_usable_password() is False  # born passwordless
        assert sp.username  # findable by /role-login/ (set on creation)

    resp = client.post(f"/api/v1/students/{sid}/credentials/", {}, format="json")
    assert resp.status_code == 200, resp.content
    data = resp.json()["data"]
    temp = data["temporary_password"]
    assert data["username"]
    assert temp

    with schema_context(tenant_a.schema_name):
        sp = StudentProfile.objects.get(pk=sid)
        assert sp.check_password(temp)
        assert sp.user.has_usable_password() is False
        assert sp.must_change_password is True  # forced to change on first login


def test_teacher_cannot_issue_credentials(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        sid = create_student(branch=branch, phone="+998905559002").id
    teacher = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    resp = teacher.post(f"/api/v1/students/{sid}/credentials/", {}, format="json")
    assert resp.status_code == 403  # students:read but not students:write


def test_username_exposed_in_detail_payload(tenant_a, user_in, as_user):
    _, client, sid = _registrar_and_student(tenant_a, user_in, as_user)
    body = client.get(f"/api/v1/students/{sid}/").json()["data"]
    assert body["username"]
    assert "user" not in body


def test_cannot_issue_credentials_for_staff_account(tenant_a, user_in, as_user):
    """Defense in depth: the student endpoint must refuse to reset a staff/superuser
    password even if a StudentProfile were linked to one."""
    _, client, sid = _registrar_and_student(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        sp = StudentProfile.objects.get(pk=sid)
        sp.user.is_staff = True
        sp.user.save(update_fields=["is_staff"])
    resp = client.post(f"/api/v1/students/{sid}/credentials/", {}, format="json")
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"
