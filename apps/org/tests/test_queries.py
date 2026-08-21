"""Query budget + pagination envelope for the Branch list (TESTING.md §3 cat 5).

The prefetch at BranchViewSet.get_queryset is only exercised when nested rows
exist, so every branch gets working-hours rows and a department. The budget is
a fixed number — a failure is an N+1 bug, never a reason to raise it.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory, BranchWorkingHoursFactory, DepartmentFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_branches_list_query_count(as_role, tenant_a, django_assert_max_num_queries):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        for _i in range(10):
            branch = BranchFactory.create()
            DepartmentFactory.create(branch=branch)
            for weekday in range(3):
                BranchWorkingHoursFactory.create(branch=branch, weekday=weekday)

    with django_assert_max_num_queries(11):  # +1: A-2 per-request permission-override load
        body = client.get("/api/v1/org/branches/").json()

    assert set(body) == {"success", "data", "pagination"}
    assert body["pagination"]["total"] >= 10
    assert any(b["working_hours"] for b in body["data"])
