"""Row-level scoping for parent/student self-service reads (TD-5).

The permission matrix grants PARENT/STUDENT the read codes; these tests pin
that the selectors then narrow the rows to own children / own profile only
(the matrix test asserts status codes; this one asserts row sets).
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.parents.models import Guardian, ParentProfile, PickupAuthorization
from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db


@pytest.fixture
def family(tenant_a, user_in):
    """Two students with different parents on one branch; returns the actors."""
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
        Guardian.objects.create(
            parent=other_parent, student=other_child, relationship="father", is_primary=True
        )
    return {
        "branch": branch,
        "own_child": own_child,
        "other_child": other_child,
        "parent_user": parent_user,
        "parent": parent,
        "other_parent": other_parent,
    }


def test_parent_sees_only_own_children(tenant_a, as_user, family):
    client = as_user(tenant_a, family["parent_user"])

    body = client.get("/api/v1/students/").json()
    assert [s["id"] for s in body["data"]] == [family["own_child"].id]

    resp = client.get(f"/api/v1/parents/{family['parent'].id}/students/")
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()["data"]] == [family["own_child"].id]


def test_parent_list_returns_only_self(tenant_a, as_user, family):
    client = as_user(tenant_a, family["parent_user"])
    body = client.get("/api/v1/parents/").json()
    assert [p["id"] for p in body["data"]] == [family["parent"].id]


def test_parent_cannot_reach_other_parents_profile(tenant_a, as_user, family):
    client = as_user(tenant_a, family["parent_user"])
    # Out of the scoped queryset -> 404, not 403 (no existence leak).
    assert client.get(f"/api/v1/parents/{family['other_parent'].id}/students/").status_code == 404


def test_parent_pickups_scoped_to_own_children(tenant_a, as_user, family):
    with schema_context(tenant_a.schema_name):
        own_pickup = PickupAuthorization.objects.create(
            student=family["own_child"], full_name="Granny", phone="+998905558801"
        )
        PickupAuthorization.objects.create(
            student=family["other_child"], full_name="Stranger", phone="+998905558802"
        )
    client = as_user(tenant_a, family["parent_user"])
    body = client.get("/api/v1/parents/pickups/").json()
    assert [p["id"] for p in body["data"]] == [own_pickup.id]


def test_student_sees_only_self(tenant_a, user_in, as_user, family):
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=family["branch"])
    with schema_context(tenant_a.schema_name):
        own_profile = StudentProfileFactory.create(user=student_user, branch=family["branch"])
    client = as_user(tenant_a, student_user)

    body = client.get("/api/v1/students/").json()
    assert [s["id"] for s in body["data"]] == [own_profile.id]
