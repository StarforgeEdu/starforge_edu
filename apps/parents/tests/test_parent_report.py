"""F2-6 — parent view of a child's report: a signed-in parent reads their OWN child's
attendance / payment / rank report (reusing the student_report selector), scoped so a
parent can never see another family's child."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.parents.models import Guardian, ParentProfile
from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

CHILDREN = "/api/v1/parents/me/children/"


@pytest.fixture
def family(tenant_a, user_in):
    """Two students with different parents on one branch."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        own_child = StudentProfileFactory.create(branch=branch)
        other_child = StudentProfileFactory.create(branch=branch)
    parent_user = user_in(tenant_a, roles=[Role.PARENT], branch=branch)
    other_parent_user = user_in(tenant_a, roles=[Role.PARENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        parent = ParentProfile.objects.create(user=parent_user)
        other_parent = ParentProfile.objects.create(user=other_parent_user)
        Guardian.objects.create(parent=parent, student=own_child, relationship="mother", is_primary=True)
        Guardian.objects.create(parent=other_parent, student=other_child, relationship="father", is_primary=True)
    return {
        "branch": branch,
        "own_child": own_child,
        "other_child": other_child,
        "parent_user": parent_user,
        "other_parent_user": other_parent_user,
    }


def test_parent_lists_only_their_own_children(tenant_a, as_user, family):
    client = as_user(tenant_a, family["parent_user"])
    body = client.get(CHILDREN).json()["data"]
    assert [s["id"] for s in body] == [family["own_child"].id]


def test_parent_reads_their_childs_report(tenant_a, as_user, family):
    client = as_user(tenant_a, family["parent_user"])
    r = client.get(f"{CHILDREN}{family['own_child'].id}/report/")
    assert r.status_code == 200, r.content
    body = r.json()["data"]
    # the same shape as the student's own /students/me/report/
    assert "attendance" in body
    assert "payment" in body
    assert "rank" in body


def test_parent_cannot_read_another_familys_child(tenant_a, as_user, family):
    """A parent guessing another student's id must get 404, never that child's report
    (no cross-family enumeration)."""
    client = as_user(tenant_a, family["parent_user"])
    r = client.get(f"{CHILDREN}{family['other_child'].id}/report/")
    assert r.status_code == 404
    assert r.json()["code"] == "not_your_child"


def test_other_parent_sees_only_their_own_child(tenant_a, as_user, family):
    client = as_user(tenant_a, family["other_parent_user"])
    listed = client.get(CHILDREN).json()["data"]
    assert [s["id"] for s in listed] == [family["other_child"].id]
    # and can read their own child's report
    assert client.get(f"{CHILDREN}{family['other_child'].id}/report/").status_code == 200


def test_a_non_parent_gets_not_a_parent(tenant_a, as_role, family):
    """A signed-in user with no parent profile (here a director) is told they're not a
    parent, rather than leaking any child data."""
    staff, _ = as_role(Role.DIRECTOR)
    r = staff.get(CHILDREN)
    assert r.status_code == 404
    assert r.json()["code"] == "not_a_parent"
    rep = staff.get(f"{CHILDREN}{family['own_child'].id}/report/")
    assert rep.status_code == 404
    assert rep.json()["code"] == "not_a_parent"
