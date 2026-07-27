"""Object-scoped permission (TASKS §26): a non-director scoped to branch A is
denied an object in branch B. Exercised against the Day-2 `TimeSlotViewSet`
(`object_scope = "branch"`)."""

from __future__ import annotations

from datetime import time
from typing import Any

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.schedule.models import TimeSlot

pytestmark = pytest.mark.django_db


def test_object_scope_branch_mismatch_denied(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch_a: Any = BranchFactory()
        branch_b: Any = BranchFactory()
        slot = TimeSlot.objects.create(
            branch=branch_b, name="P1", start_time=time(9, 0), end_time=time(10, 0)
        )
        slot_id = slot.id

    # Teacher membership is in branch A; the slot lives in branch B.
    teacher = user_in(tenant_a, roles=["teacher"], branch=branch_a)
    resp = as_user(tenant_a, teacher).get(f"/api/v1/schedule/timeslots/{slot_id}/")
    assert resp.status_code == 403  # passes schedule:read gate, fails branch object scope


def test_object_scope_director_bypass(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch_a: Any = BranchFactory()
        branch_b: Any = BranchFactory()
        slot = TimeSlot.objects.create(
            branch=branch_b, name="P2", start_time=time(10, 0), end_time=time(11, 0)
        )
        slot_id = slot.id

    director = user_in(tenant_a, roles=["director"], branch=branch_a)
    resp = as_user(tenant_a, director).get(f"/api/v1/schedule/timeslots/{slot_id}/")
    assert resp.status_code == 200  # director bypasses object scope
