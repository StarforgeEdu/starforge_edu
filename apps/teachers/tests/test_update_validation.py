"""TeacherUpdateSerializer cross-field guard: department must belong to the
teacher's branch on update too (mirrors create_teacher, D1-LD-8)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory, DepartmentFactory
from apps.teachers.services import create_teacher
from core.permissions import Role

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        other_branch = BranchFactory.create()
        dept = DepartmentFactory.create(branch=branch)
        other_dept = DepartmentFactory.create(branch=other_branch)
        teacher = create_teacher(branch=branch, department=dept, phone="+998905557001")
    return {"branch": branch, "other_branch": other_branch, "other_dept": other_dept, "teacher": teacher}


def test_patch_cross_branch_department_400(as_role, setup):
    client, _ = as_role(Role.DIRECTOR)
    url = f"/api/v1/teachers/{setup['teacher'].id}/"

    # Department from another branch (branch unchanged) -> 400.
    resp = client.patch(url, {"department": setup["other_dept"].id}, format="json")
    assert resp.status_code == 400
    assert "department" in resp.json()["errors"]

    # Branch changes cannot bypass the audited transfer workflow.
    resp = client.patch(url, {"branch": setup["other_branch"].id}, format="json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "use_branch_transfer"
    assert "branch" in resp.json()["errors"]


def test_patch_branch_change_requires_transfer_workflow(as_role, setup, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    url = f"/api/v1/teachers/{setup['teacher'].id}/"

    # Department maintenance remains available on the profile itself.
    resp = client.patch(url, {"department": None}, format="json")
    assert resp.status_code == 200

    # Moving the branch is an audited transaction and cannot bypass the
    # dedicated transfer service, even when the target department is valid.
    resp = client.patch(
        url,
        {"branch": setup["other_branch"].id, "department": setup["other_dept"].id},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "use_branch_transfer"
    assert "branch" in resp.json()["errors"]
    with schema_context(tenant_a.schema_name):
        setup["teacher"].refresh_from_db()
        assert setup["teacher"].branch_id == setup["branch"].id
        assert setup["teacher"].department_id is None
