"""Privacy and contract tests for the staff mobile availability projection."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/org/app-status/"


def _principal_client(client_for, tenant, account, *, kind: str, read_only: bool = False):
    from core.session_auth import create_session

    with schema_context(tenant.schema_name):
        session = create_session(
            account.user,
            principal_kind=kind,
            principal_id=account.pk,
            read_only=read_only,
        )
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
    return client


def _teacher_account(tenant):
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant.schema_name):
        teacher = TeacherProfileFactory()
        RoleMembership.objects.create(
            user=teacher.user,
            branch=teacher.branch,
            role=Role.TEACHER,
        )
        return teacher


def _staff_account(tenant, *, role: str):
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        user = UserFactory()
        branch = BranchFactory()
        staff = StaffProfile.objects.create(
            user=user,
            username=f"staff.{user.username}",
            password=user.password,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        RoleMembership.objects.create(user=user, branch=branch, role=role)
        return staff


def test_teacher_reads_only_normalized_product_statuses(tenant_a, client_for, monkeypatch):
    from core import availability

    teacher = _teacher_account(tenant_a)
    states = {
        "ai": availability.STATUS_DISABLED,
        "notifications": availability.STATUS_DEGRADED,
    }
    monkeypatch.setattr(
        availability,
        "resolve_status",
        lambda app: (states.get(app, availability.STATUS_UP), ["private dependency detail"]),
    )

    response = _principal_client(client_for, tenant_a, teacher, kind="teacher").get(URL)

    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "no-store"
    rows = response.json()["data"]["features"]
    assert {row["feature"]: row["status"] for row in rows} == {
        "ai": "unavailable",
        "notifications": "degraded",
        "groups": "available",
        "attendance": "available",
        "library": "available",
        "printing": "available",
        "messaging": "available",
        "tasks": "available",
    }
    assert all(set(row) == {"feature", "status"} for row in rows)
    assert "private dependency detail" not in response.content.decode()


def test_read_only_staff_session_can_read_status(tenant_a, client_for):
    staff = _staff_account(tenant_a, role=Role.SUPPORT)
    response = _principal_client(
        client_for,
        tenant_a,
        staff,
        kind="staff",
        read_only=True,
    ).get(URL)
    assert response.status_code == 200, response.content


@pytest.mark.parametrize("role", [Role.DIRECTOR, Role.HEAD_OF_DEPT])
def test_executive_staff_accounts_are_rejected(tenant_a, client_for, role):
    staff = _staff_account(tenant_a, role=role)
    response = _principal_client(client_for, tenant_a, staff, kind="staff").get(URL)
    assert response.status_code == 403
    assert response.json()["code"] == "staff_app_account_required"


def test_student_role_account_is_rejected(tenant_a, client_for):
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    response = _principal_client(client_for, tenant_a, student, kind="student").get(URL)
    assert response.status_code == 403
    assert response.json()["code"] == "staff_app_account_required"


def test_status_contract_is_closed_and_authenticated():
    from core.openapi import build_schema

    operation = build_schema(None)["paths"][URL]["get"]
    assert operation["security"]
    envelope = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert envelope["additionalProperties"] is False
    item = envelope["properties"]["data"]["properties"]["features"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["status"]["enum"] == [
        "available",
        "degraded",
        "unavailable",
    ]
