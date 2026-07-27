"""Parent-domain CRUD over the layered (off-DRF) views: success/data + paginated
envelopes, guardian/pickup create+delete, method restrictions, and write authz.
Complements test_scoping (row scoping) and test_parent_report (self-service)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db
PARENTS = "/api/v1/parents/"
GUARDIANS = "/api/v1/parents/guardians/"
PICKUPS = "/api/v1/parents/pickups/"


def test_director_parent_create_list_retrieve_update_delete(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)

    resp = client.post(
        PARENTS, {"phone": "+998905553001", "first_name": "Ada", "workplace": "Acme"}, format="json"
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["success"] is True
    pid = body["data"]["id"]
    assert body["data"]["first_name"] == "Ada"
    assert "user" not in body["data"]
    assert body["data"]["workplace"] == "Acme"

    listed = client.get(PARENTS).json()
    assert "pagination" in listed
    assert any(p["id"] == pid for p in listed["data"])

    assert client.get(f"{PARENTS}{pid}/").json()["data"]["id"] == pid

    upd = client.patch(f"{PARENTS}{pid}/", {"workplace": "NewCo"}, format="json")
    assert upd.status_code == 200
    assert upd.json()["data"]["workplace"] == "NewCo"

    assert client.delete(f"{PARENTS}{pid}/").status_code == 204
    assert client.get(f"{PARENTS}{pid}/").status_code == 404


def test_create_requires_phone_or_email(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)
    resp = client.post(PARENTS, {"first_name": "NoContact"}, format="json")
    assert resp.status_code == 422
    assert "phone" in resp.json()["errors"]


def test_guardian_link_create_list_delete_and_no_update(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.services import create_parent
    from apps.students.services import create_student

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = create_student(branch=branch, phone="+998905553010")
        parent = create_parent(phone="+998905553011")

    resp = client.post(
        GUARDIANS,
        {"parent": parent.id, "student": student.id, "relationship": "mother", "is_primary": True},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    gid = resp.json()["data"]["id"]
    assert resp.json()["data"]["is_primary"] is True

    listed = client.get(GUARDIANS).json()
    assert any(g["id"] == gid for g in listed["data"])

    # Links are create+delete only — no PUT/PATCH.
    assert client.put(f"{GUARDIANS}{gid}/", {"relationship": "father"}, format="json").status_code == 405
    assert client.delete(f"{GUARDIANS}{gid}/").status_code == 204


def test_guardian_duplicate_link_is_400(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.services import create_parent
    from apps.students.services import create_student

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = create_student(branch=branch, phone="+998905553020")
        parent = create_parent(phone="+998905553021")
    payload = {"parent": parent.id, "student": student.id, "relationship": "mother"}
    assert client.post(GUARDIANS, payload, format="json").status_code == 201
    dup = client.post(GUARDIANS, payload, format="json")
    assert dup.status_code == 400
    assert dup.json()["code"] == "guardian_exists"


def test_guardian_list_includes_readable_names(tenant_a, as_role):
    """The guardian list denormalizes parent/student ids to readable `*_name`
    companions (resolved from select_related — no second call needed)."""
    from apps.org.tests.factories import BranchFactory
    from apps.parents.services import create_parent
    from apps.students.services import create_student

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = create_student(branch=branch, phone="+998905553060", first_name="Kid", last_name="Smith")
        parent = create_parent(phone="+998905553061", first_name="Ada", last_name="Lovelace")
        expected_parent_name = parent.user.get_full_name()
        expected_student_name = student.user.get_full_name()

    resp = client.post(
        GUARDIANS,
        {"parent": parent.id, "student": student.id, "relationship": "mother"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    gid = resp.json()["data"]["id"]

    row = next(g for g in client.get(GUARDIANS).json()["data"] if g["id"] == gid)
    assert row["parent"] == parent.id
    assert row["parent_name"] == expected_parent_name
    assert row["student"] == student.id
    assert row["student_name"] == expected_student_name


def test_pickup_create_and_list(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory
    from apps.students.services import create_student

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = create_student(branch=branch, phone="+998905553030")

    resp = client.post(
        PICKUPS,
        {"student": student.id, "full_name": "Granny", "phone": "+998905553031"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    kid = resp.json()["data"]["id"]
    assert resp.json()["data"]["is_active"] is True
    assert any(p["id"] == kid for p in client.get(PICKUPS).json()["data"])


def test_pickup_create_requires_full_name(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory
    from apps.students.services import create_student

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = create_student(branch=branch, phone="+998905553040")
    resp = client.post(PICKUPS, {"student": student.id, "phone": "+998905553041"}, format="json")
    assert resp.status_code == 422
    assert "full_name" in resp.json()["errors"]


def test_parent_role_cannot_write(tenant_a, user_in, as_user):
    """PARENT holds parents:read (self-service) but NOT parents:write — creating a
    parent must be 403."""
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.PARENT]))
    resp = client.post(PARENTS, {"phone": "+998905553050"}, format="json")
    assert resp.status_code == 403
