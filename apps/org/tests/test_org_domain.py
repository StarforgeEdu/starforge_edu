"""Lane F mandated endpoint tests (DAY-1 / DoD #10).

Seven of the eight mandated tests live here; the eighth —
test_branches_list_query_count (Branch list with nested hours) — lives in
apps/org/tests/test_queries.py per the query-budget test layout.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.models import Branch, BranchWorkingHours, Room
from apps.org.services import set_department_head
from apps.org.tests.factories import BranchFactory, DepartmentFactory, RoomFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.users.tests.factories import UserFactory
from core.exceptions import ValidationException
from core.permissions import Role

pytestmark = pytest.mark.django_db

WEEK = [{"weekday": d, "opens_at": "08:00", "closes_at": "18:00", "is_closed": False} for d in range(7)]


def test_room_crud_and_branch_scope(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        b1 = BranchFactory.create()
        b2 = BranchFactory.create()
        RoomFactory.create(branch=b2, name="Other-branch room")

    resp = client.post("/api/v1/org/rooms/", {"branch": b1.id, "name": "R1", "capacity": 20}, format="json")
    assert resp.status_code == 201
    room_id = resp.json()["data"]["id"]

    body = client.get(f"/api/v1/org/rooms/?branch={b1.id}").json()
    assert [r["id"] for r in body["data"]] == [room_id]

    resp = client.patch(f"/api/v1/org/rooms/{room_id}/", {"capacity": 25}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["capacity"] == 25

    # Duplicate (branch, name) -> 400 envelope (UniqueTogetherValidator).
    dup = client.post("/api/v1/org/rooms/", {"branch": b1.id, "name": "R1"}, format="json")
    assert dup.status_code == 400
    assert dup.json()["code"] == "validation_error"

    assert client.delete(f"/api/v1/org/rooms/{room_id}/").status_code == 204
    with schema_context(tenant_a.schema_name):
        assert not Room.objects.filter(pk=room_id).exists()


@pytest.mark.parametrize("role", [Role.TEACHER, Role.CASHIER])
def test_room_write_denied(as_role, tenant_a, role):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    client, _ = as_role(role)
    resp = client.post("/api/v1/org/rooms/", {"branch": branch.id, "name": "Nope"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


def test_working_hours_bulk_replace_atomic(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    url = f"/api/v1/org/branches/{branch.id}/working-hours/"

    assert client.put(url, WEEK, format="json").status_code == 200
    new_week = [{**row, "opens_at": "09:00"} for row in WEEK]
    resp = client.put(url, new_week, format="json")
    assert resp.status_code == 200
    with schema_context(tenant_a.schema_name):
        rows = list(BranchWorkingHours.objects.filter(branch=branch).order_by("weekday"))
        assert len(rows) == 7
        assert all(str(r.opens_at) == "09:00:00" for r in rows)

    # Duplicate weekday -> 400 invalid_working_hours, prior rows intact (atomic).
    dup_week = [*new_week, {**new_week[0]}]
    resp = client.put(url, dup_week, format="json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_working_hours"
    with schema_context(tenant_a.schema_name):
        assert BranchWorkingHours.objects.filter(branch=branch).count() == 7


def test_working_hours_invalid_range_400(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    bad = [{"weekday": 0, "opens_at": "18:00", "closes_at": "08:00", "is_closed": False}]
    resp = client.put(f"/api/v1/org/branches/{branch.id}/working-hours/", bad, format="json")
    assert resp.status_code == 400


def test_holiday_unique_per_branch_date(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    url = f"/api/v1/org/branches/{branch.id}/holidays/"
    payload = {"date": "2026-09-01", "name": "Independence Day"}

    assert client.post(url, payload, format="json").status_code == 201
    dup = client.post(url, payload, format="json")
    assert dup.status_code == 409
    assert dup.json()["code"] == "holiday_exists"


def test_department_head_late_validation(tenant_a):
    """D1-LF-4: now that teachers.TeacherProfile exists, a non-teacher head is
    rejected at the service level (un-skipped per DAY-1's same-day rule)."""
    with schema_context(tenant_a.schema_name):
        dept = DepartmentFactory.create()
        non_teacher = UserFactory.create()
        with pytest.raises(ValidationException) as exc:
            set_department_head(dept, non_teacher)
        assert exc.value.code == "head_not_teacher"


def test_branch_archive_instead_of_delete(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        occupied = BranchFactory.create()
        StudentProfileFactory.create(branch=occupied)  # status=active
        empty = BranchFactory.create()

    resp = client.delete(f"/api/v1/org/branches/{occupied.id}/")
    assert resp.status_code == 409
    assert resp.json()["code"] == "branch_has_active_students"

    assert client.delete(f"/api/v1/org/branches/{empty.id}/").status_code == 204
    with schema_context(tenant_a.schema_name):
        empty.refresh_from_db()  # soft delete: the row survives
        assert empty.archived_at is not None
        assert empty.is_active is False
        assert Branch.objects.filter(pk=occupied.id, archived_at__isnull=True).exists()


def test_branch_archived_excluded_from_list(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        visible = BranchFactory.create()
        archived = BranchFactory.create()
    assert client.delete(f"/api/v1/org/branches/{archived.id}/").status_code == 204

    ids = [b["id"] for b in client.get("/api/v1/org/branches/").json()["data"]]
    assert visible.id in ids
    assert archived.id not in ids
