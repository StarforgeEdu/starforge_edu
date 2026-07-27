"""Cohort CRUD over the layered (off-DRF) views: success/data + paginated
envelopes, branch scoping, and per-perm authz. Complements test_membership /
test_branch_scope (which cover the enroll/move/archive action semantics)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db
URL = "/api/v1/cohorts/"


def test_director_create_list_retrieve_delete(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()

    resp = client.post(
        URL,
        {
            "name": "Morning A1",
            "branch": branch.id,
            "start_date": "2026-01-01",
            "end_date": "2026-06-30",
            "level": "A1",
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["success"] is True
    cid = body["data"]["id"]
    assert body["data"]["name"] == "Morning A1"
    assert body["data"]["co_teachers"] == []

    listed = client.get(URL).json()
    assert listed["success"] is True
    assert "pagination" in listed
    assert any(c["id"] == cid for c in listed["data"])

    one = client.get(f"{URL}{cid}/").json()
    assert one["data"]["id"] == cid

    assert client.delete(f"{URL}{cid}/").status_code == 204  # empty + unarchived -> deletable
    assert client.get(f"{URL}{cid}/").status_code == 404


def test_create_rejects_end_before_start(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = client.post(
        URL,
        {"name": "Bad", "branch": branch.id, "start_date": "2026-06-30", "end_date": "2026-01-01"},
        format="json",
    )
    assert resp.status_code == 400
    assert "end_date" in resp.json()["errors"]


def test_create_rejects_cross_branch_relationships(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory, RoomFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        other = BranchFactory()
        department = DepartmentFactory(branch=other)
        room = RoomFactory(branch=other)
        teacher = TeacherProfileFactory(branch=other)

    response = client.post(
        URL,
        {
            "name": "Mixed branch",
            "branch": branch.id,
            "start_date": "2026-01-01",
            "end_date": "2026-06-30",
            "department": department.id,
            "default_room": room.id,
            "primary_teacher": teacher.id,
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "cross_branch_relationship"
    assert set(response.json()["errors"]) == {"department", "default_room", "primary_teacher"}


def test_update_rejects_blank_name_negative_capacity_and_cross_branch_room(tenant_a, as_role):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory, RoomFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        other = BranchFactory()
        cohort = CohortFactory(branch=branch, name="Valid", capacity=12)
        other_room = RoomFactory(branch=other)
        other_teacher = TeacherProfileFactory(branch=other)

    for body, field in (
        ({"name": "   "}, "name"),
        ({"capacity": -1}, "capacity"),
        ({"default_room": other_room.id}, "default_room"),
    ):
        response = client.patch(f"{URL}{cohort.id}/", body, format="json")
        assert response.status_code == 400
        assert field in response.json()["errors"]

    teacher_response = client.post(
        f"{URL}{cohort.id}/teachers/", {"teacher": other_teacher.id}, format="json"
    )
    assert teacher_response.status_code == 400
    assert "teacher" in teacher_response.json()["errors"]

    with schema_context(tenant_a.schema_name):
        cohort.refresh_from_db()
        assert cohort.name == "Valid"
        assert cohort.capacity == 12
        assert cohort.default_room_id is None


def test_list_is_branch_scoped(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        mine = CohortFactory(branch=branch_a)
        theirs = CohortFactory(branch=branch_b)
    # A registrar scoped to branch_a (cohorts:read, non-director) sees only branch_a.
    client = as_user(tenant_a, user_in(tenant_a, roles=["registrar"], branch=branch_a))
    ids = {c["id"] for c in client.get(URL).json()["data"]}
    assert mine.id in ids
    assert theirs.id not in ids


def test_detail_out_of_scope_is_403(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        theirs = CohortFactory(branch=branch_b)
    client = as_user(tenant_a, user_in(tenant_a, roles=["registrar"], branch=branch_a))
    assert client.get(f"{URL}{theirs.id}/").status_code == 403


def test_role_without_cohorts_read_is_denied(tenant_a, as_role):
    client, _ = as_role(Role.CASHIER)  # cashier holds no cohorts permission
    assert client.get(URL).status_code == 403


def test_list_row_carries_readable_name_companions(tenant_a, as_role):
    """Each bare FK id on a list row ships a readable `_name` companion (branch /
    department / primary_teacher / default_room) so a client renders a cohort without a
    second call — select_related keeps it N+1-free."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory, DepartmentFactory, RoomFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        room = RoomFactory(branch=branch)
        teacher = TeacherProfileFactory(branch=branch)
        cohort = CohortFactory(
            branch=branch, department=department, primary_teacher=teacher, default_room=room
        )
        expected_teacher_name = teacher.user.get_full_name()

    rows = client.get(URL).json()["data"]
    row = next(c for c in rows if c["id"] == cohort.id)
    assert row["branch_name"] == branch.name
    assert row["department_name"] == department.name
    assert row["primary_teacher_name"] == expected_teacher_name
    assert row["default_room_name"] == room.name
