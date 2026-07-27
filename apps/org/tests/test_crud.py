"""Org CRUD over the layered (off-DRF) views: branch create/detail via the API,
the read-only transfers endpoint, and department branch-scoping. Complements
test_org_domain (rooms/hours/holidays/archive), test_settings, test_departments."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_branch_create_list_retrieve_update(as_role):
    client, _ = as_role(Role.DIRECTOR)

    resp = client.post("/api/v1/org/branches/", {"name": "Downtown", "slug": "downtown"}, format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["success"] is True
    bid = body["data"]["id"]
    assert body["data"]["name"] == "Downtown"
    assert body["data"]["departments"] == []
    assert body["data"]["working_hours"] == []

    listed = client.get("/api/v1/org/branches/").json()
    assert "pagination" in listed
    assert any(b["id"] == bid for b in listed["data"])

    detail = client.get(f"/api/v1/org/branches/{bid}/").json()["data"]
    assert detail["id"] == bid
    assert "capacity_status" in detail  # detail-only field

    upd = client.patch(f"/api/v1/org/branches/{bid}/", {"phone": "+998901112233"}, format="json")
    assert upd.status_code == 200
    assert upd.json()["data"]["phone"] == "+998901112233"


def test_branch_create_requires_name_and_slug(as_role):
    """DRF's ModelSerializer enforced required fields; the layered create must too."""
    client, _ = as_role(Role.DIRECTOR)
    resp = client.post("/api/v1/org/branches/", {"name": "NoSlug"}, format="json")
    assert resp.status_code == 400
    assert "slug" in resp.json()["errors"]


def test_branch_rejects_invalid_slug(as_role):
    client, _ = as_role(Role.DIRECTOR)
    resp = client.post("/api/v1/org/branches/", {"name": "X", "slug": "not a slug!"}, format="json")
    assert resp.status_code == 400
    assert "slug" in resp.json()["errors"]


def test_room_capacity_out_of_range_is_400_not_500(as_role, tenant_a):
    """An out-of-range capacity (> PositiveSmallInteger max) must be a clean 400,
    never a 500 DataError from Postgres."""
    from apps.org.tests.factories import BranchFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = client.post(
        "/api/v1/org/rooms/", {"branch": branch.id, "name": "Huge", "capacity": 99999}, format="json"
    )
    assert resp.status_code == 400


def test_branch_write_denied_for_teacher(as_role, tenant_a):
    """Teacher holds org:read (GET 200) but not org:write (create 403)."""
    client, _ = as_role(Role.TEACHER)
    assert client.get("/api/v1/org/branches/").status_code == 200
    resp = client.post("/api/v1/org/branches/", {"name": "X", "slug": "x"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


def test_transfers_are_read_only(as_role, tenant_a):
    from apps.org.services import record_transfer
    from apps.org.tests.factories import BranchFactory
    from apps.users.tests.factories import UserFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        a = BranchFactory()
        b = BranchFactory()
        record_transfer(user=UserFactory(), from_branch=a, to_branch=b, reason="moved")

    listed = client.get("/api/v1/org/transfers/").json()
    assert "pagination" in listed
    assert len(listed["data"]) >= 1
    assert listed["data"][0]["from_branch"] == a.id
    # Read-only: writes are 405.
    assert client.post("/api/v1/org/transfers/", {}, format="json").status_code == 405


def test_department_list_surfaces_readable_fk_names(as_role, tenant_a):
    """The departments list must carry branch_name + head_name next to the bare
    branch/head ids so a client needn't make a second call. branch/head are
    select_related on the list queryset, so this adds JOINs, not queries."""
    from apps.org.services import set_department_head
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.teachers.services import create_teacher

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Central Campus")
        dept = DepartmentFactory(branch=branch)
        teacher = create_teacher(branch=branch, phone="+998905559050", first_name="Dana")
        set_department_head(dept, teacher)
        expected_head = teacher.get_full_name()
        head_teacher_id = teacher.id

    row = next(d for d in client.get("/api/v1/org/departments/").json()["data"] if d["id"] == dept.id)
    assert row["branch"] == branch.id
    assert row["branch_name"] == "Central Campus"
    assert row["head"] == head_teacher_id
    assert row["head_name"] == expected_head


def test_department_list_and_detail_branch_scoped(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        mine = DepartmentFactory(branch=branch_a)
        theirs = DepartmentFactory(branch=branch_b)
    # A teacher (org:read, non-director) scoped to branch_a sees only branch_a depts.
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a))
    ids = {d["id"] for d in client.get("/api/v1/org/departments/").json()["data"]}
    assert mine.id in ids
    assert theirs.id not in ids
    # And a cross-branch detail read is 403 (out of scope), never a leak.
    assert client.get(f"/api/v1/org/departments/{theirs.id}/").status_code == 403
