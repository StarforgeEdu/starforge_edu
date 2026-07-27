"""Plan-limit paywall on the student CREATE path (TD-8 / D3-E-7).

create_student() at a seat-consuming status (enrolled/active) acquires a seat
just like the LEAD->...->ENROLLED transition, so it must honour the plan's
max_students cap. Creating the (max+1)th student directly at ACTIVE must 402
`plan_limit_exceeded` — otherwise a students:write role bypasses the paywall by
POSTing {"status": "active"} repeatedly.

Subscription/Plan are PUBLIC-schema rows: create them WITHOUT a schema_context
(the autouse `_reset_schema_to_public` fixture leaves us on public). The
subscription MUST be `active` so SubscriptionGateMiddleware does not paywall the
domain API with `subscription_required` before the create view runs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.billing.models import Subscription
from apps.billing.services import PlanLimitExceeded
from apps.billing.tests.factories import PlanFactory
from apps.org.tests.factories import BranchFactory
from apps.students.models import StudentProfile
from apps.students.services import create_student
from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

S = StudentProfile.Status


def _cap_subscription(center, *, max_students):
    """Active subscription on a capped plan (public schema)."""
    plan = PlanFactory(code=f"cap-{max_students}", max_students=max_students, price_uzs=0)
    Subscription.objects.update_or_create(
        center=center,
        defaults={
            "plan": plan,
            "status": Subscription.Status.ACTIVE,
            "current_period_start": timezone.now() - timedelta(days=1),
            "current_period_end": timezone.now() + timedelta(days=30),
        },
    )
    cache.delete(f"billing:subscription_status:{center.schema_name}")


# --------------------------------------------------------------------------- #
# Service-level: the seat-consuming create honours the cap.
# --------------------------------------------------------------------------- #
def test_create_at_active_enforces_plan_limit(tenant_a):
    _cap_subscription(tenant_a, max_students=2)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        # Fill the cap with 2 active students (factory rows, no enforcement).
        StudentProfileFactory.create_batch(2, status=S.ACTIVE, branch=branch)
        # The 3rd, created directly at ACTIVE, must hit the cap.
        with pytest.raises(PlanLimitExceeded) as exc:
            create_student(branch=branch, phone="+998905554001", status=S.ACTIVE)
    assert exc.value.code == "plan_limit_exceeded"
    assert exc.value.status_code == 402
    with schema_context(tenant_a.schema_name):
        assert StudentProfile.objects.filter(status=S.ACTIVE).count() == 2  # rolled back


def test_create_at_enrolled_enforces_plan_limit(tenant_a):
    _cap_subscription(tenant_a, max_students=1)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        StudentProfileFactory.create(status=S.ENROLLED, branch=branch)
        with pytest.raises(PlanLimitExceeded):
            create_student(branch=branch, phone="+998905554002", status=S.ENROLLED)


def test_create_as_lead_does_not_consume_a_seat(tenant_a):
    """A LEAD is not a seat state, so creating one at/over the cap must NOT 402."""
    _cap_subscription(tenant_a, max_students=1)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        StudentProfileFactory.create(status=S.ACTIVE, branch=branch)  # cap full of seats
        student = create_student(branch=branch, phone="+998905554003", status=S.LEAD)
        assert student.status == S.LEAD


def test_skip_limit_check_bypasses_enforcement(tenant_a):
    """The seed/import opt-out must still create past the cap (no 402)."""
    _cap_subscription(tenant_a, max_students=1)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        StudentProfileFactory.create(status=S.ACTIVE, branch=branch)
        student = create_student(branch=branch, phone="+998905554004", status=S.ACTIVE, skip_limit_check=True)
        assert student.status == S.ACTIVE


# --------------------------------------------------------------------------- #
# API-level: POST /students/ {"status":"active"} past the cap returns 402.
# --------------------------------------------------------------------------- #
def test_api_create_at_active_past_cap_returns_402(tenant_a, user_in, as_user):
    _cap_subscription(tenant_a, max_students=1)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        StudentProfileFactory.create(status=S.ACTIVE, branch=branch)  # cap reached
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    resp = client.post(
        "/api/v1/students/",
        {"branch": branch.id, "phone": "+998905554005", "status": "active"},
        format="json",
    )
    assert resp.status_code == 402
    assert resp.json()["code"] == "plan_limit_exceeded"
    # No student was persisted for the rejected create.
    with schema_context(tenant_a.schema_name):
        assert not StudentProfile.objects.filter(user__phone="+998905554005").exists()


def test_api_create_below_cap_succeeds(tenant_a, user_in, as_user):
    _cap_subscription(tenant_a, max_students=5)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    resp = client.post(
        "/api/v1/students/",
        {"branch": branch.id, "phone": "+998905554006", "status": "active"},
        format="json",
    )
    assert resp.status_code == 201
