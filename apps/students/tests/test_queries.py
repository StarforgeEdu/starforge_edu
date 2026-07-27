"""Query budget + pagination envelope for the student list (TESTING.md §3 cat 5).

Pins both the selector eager-loading (apps/students/selectors.py) and the
per-request role-membership cache (core/permissions.py) — a fixed budget at
50 rows fails on any N+1 or repeated RoleMembership query.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_students_list_query_count(as_role, tenant_a, django_assert_max_num_queries):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        StudentProfileFactory.create_batch(50)

    with django_assert_max_num_queries(10):  # fixed budget; MUST NOT scale with rows
        body = client.get("/api/v1/students/").json()

    assert set(body) == {"success", "data", "pagination"}
    assert body["pagination"]["total"] >= 50
