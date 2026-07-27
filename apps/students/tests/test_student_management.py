"""Feature 2 — student list page: profile fields, block/unblock, filters,
stats, comparison. Built against agents/FEATURE_BACKLOG.md (F2-*)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.students.services import create_student
from core.permissions import Role

pytestmark = pytest.mark.django_db


def _branch_and_client(tenant, user_in, as_user, role=Role.REGISTRAR):
    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    client = as_user(tenant, user_in(tenant, roles=[role], branch=branch))
    return branch, client


# --------------------------------------------------------------------------- #
# F2-1 — profile fields (location, previous_school) + is_blocked flag
# --------------------------------------------------------------------------- #
def test_create_and_read_location_and_previous_school(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    resp = client.post(
        "/api/v1/students/",
        {
            "phone": "+998905557001",
            "branch": branch.pk,
            "location": "Tashkent, Yunusabad",
            "previous_school": "School #110",
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content
    sid = resp.json()["data"]["id"]
    body = client.get(f"/api/v1/students/{sid}/").json()["data"]
    assert body["location"] == "Tashkent, Yunusabad"
    assert body["previous_school"] == "School #110"
    assert body["is_blocked"] is False
    assert body["blocked_at"] is None


# --------------------------------------------------------------------------- #
# F2-2 — block / unblock
# --------------------------------------------------------------------------- #
def test_block_then_unblock_student(tenant_a, user_in, as_user):
    branch, client = _branch_and_client(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        student = create_student(branch=branch, phone="+998905557010")

    resp = client.post(f"/api/v1/students/{student.id}/block/", {"reason": "unpaid balance"}, format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()["data"]
    assert body["is_blocked"] is True
    assert body["blocked_at"] is not None
    assert body["block_reason"] == "unpaid balance"

    resp = client.post(f"/api/v1/students/{student.id}/unblock/", {}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["is_blocked"] is False


def test_block_requires_write_role(tenant_a, user_in, as_user):
    branch, _ = _branch_and_client(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        student = create_student(branch=branch, phone="+998905557011")
    # a teacher has students:read but not students:write -> 403
    teacher = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    resp = teacher.post(f"/api/v1/students/{student.id}/block/", {"reason": "x"}, format="json")
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# F2-3 — rich filters
# --------------------------------------------------------------------------- #
def test_student_filters(tenant_a, user_in, as_user):
    branch, client = _branch_and_client(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        a = create_student(branch=branch, phone="+998905557020", location="Tashkent", academic_level="A1")
        create_student(branch=branch, phone="+998905557021", location="Samarkand", academic_level="B2")

    def ids(query):
        return {r["id"] for r in client.get(f"/api/v1/students/?{query}").json()["data"]}

    assert ids("location=tash") == {a.id}
    assert ids("level=a1") == {a.id}  # iexact, case-insensitive
    assert len(ids("has_cohort=false")) == 2  # neither is enrolled in a cohort

    client.post(f"/api/v1/students/{a.id}/block/", {"reason": "x"}, format="json")
    assert ids("blocked=true") == {a.id}
    assert a.id not in ids("blocked=false")

    # garbage typed param -> 400, never a 500
    assert client.get("/api/v1/students/?age_min=abc").status_code == 400


# --------------------------------------------------------------------------- #
# F2-4 — stats snapshot
# --------------------------------------------------------------------------- #
def test_stats_snapshot(tenant_a, user_in, as_user):
    branch, client = _branch_and_client(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        a = create_student(branch=branch, phone="+998905557030")
        create_student(branch=branch, phone="+998905557031")
    client.post(f"/api/v1/students/{a.id}/block/", {"reason": "x"}, format="json")

    body = client.get("/api/v1/students/stats/").json()["data"]
    assert body["total"] == 2
    assert body["without_cohort"] == 2
    assert body["with_cohort"] == 0
    assert body["blocked"] == 1
    assert body["by_status"]["lead"] == 2


# --------------------------------------------------------------------------- #
# F2-5 — period comparison
# --------------------------------------------------------------------------- #
def test_comparison_joined_and_left(tenant_a, user_in, as_user):
    from apps.students.models import StudentProfile
    from apps.students.services import transition_enrollment

    branch, client = _branch_and_client(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        create_student(branch=branch, phone="+998905557040")  # a fresh "join"
        leaver = create_student(branch=branch, phone="+998905557041", status=StudentProfile.Status.ACTIVE)
        transition_enrollment(student=leaver, to_status=StudentProfile.Status.WITHDRAWN)

    joined = client.get("/api/v1/students/comparison/?metric=joined&unit=year").json()["data"]
    assert joined["current"] == 2  # both records were created this year
    assert joined["previous"] == 0

    left = client.get("/api/v1/students/comparison/?metric=left&unit=year").json()["data"]
    assert left["current"] == 1  # one withdrawal this year
    assert left["unit"] == "year"

    # bad enum -> 400
    assert client.get("/api/v1/students/comparison/?unit=decade").status_code == 400
