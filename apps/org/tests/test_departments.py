"""Department head validation (D1-LF-4 / D1-LD-10) — service and API surfaces."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.services import set_department_head
from apps.org.tests.factories import BranchFactory, DepartmentFactory
from apps.teachers.services import create_teacher
from apps.users.tests.factories import UserFactory
from core.exceptions import ValidationException
from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_set_department_head_requires_teacher_profile(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        dept = DepartmentFactory.create(branch=branch)

        non_teacher = UserFactory.create()
        with pytest.raises(ValidationException) as exc:
            set_department_head(dept, non_teacher)
        assert exc.value.code == "head_not_teacher"
        dept.refresh_from_db()
        assert dept.head is None

        teacher = create_teacher(branch=branch, phone="+998905559001", first_name="Head")
        set_department_head(dept, teacher)
        dept.refresh_from_db()
        assert dept.head_id == teacher.user_id

        # Clearing the head is always allowed.
        set_department_head(dept, None)
        dept.refresh_from_db()
        assert dept.head is None


def test_department_head_must_belong_to_the_same_branch(tenant_a):
    with schema_context(tenant_a.schema_name):
        department_branch = BranchFactory.create()
        other_branch = BranchFactory.create()
        dept = DepartmentFactory.create(branch=department_branch)
        foreign_teacher = create_teacher(
            branch=other_branch,
            phone="+998905559003",
            first_name="Foreign Head",
        )

        with pytest.raises(ValidationException) as exc:
            set_department_head(dept, foreign_teacher)

        assert exc.value.code == "head_branch_mismatch"
        dept.refresh_from_db()
        assert dept.head_id is None


def test_patch_department_head_validated_at_api(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        dept = DepartmentFactory.create(branch=branch)
        UserFactory.create()
        teacher = create_teacher(branch=branch, phone="+998905559002", first_name="Head")
    url = f"/api/v1/org/departments/{dept.id}/"

    resp = client.patch(url, {"head": 999999}, format="json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_head"

    resp = client.patch(url, {"head": teacher.id}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["head"] == teacher.id


def test_patch_department_rejects_foreign_branch_head(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        department_branch = BranchFactory.create()
        other_branch = BranchFactory.create()
        dept = DepartmentFactory.create(branch=department_branch)
        foreign_teacher = create_teacher(
            branch=other_branch,
            phone="+998905559004",
            first_name="Foreign Head",
        )

    response = client.patch(
        f"/api/v1/org/departments/{dept.id}/",
        {"head": foreign_teacher.id},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "head_branch_mismatch"
