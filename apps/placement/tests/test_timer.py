"""F8-2 (timer) — a timed placement test: an assigned attempt gets a deadline; a
submit after the deadline is rejected (the lead ran out of time). Untimed tests
(no time_limit_minutes) have no deadline.
"""

from __future__ import annotations

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TESTS = "/api/v1/placement/tests/"
ATTEMPTS = "/api/v1/placement/attempts/"


def _approved_test(tenant, branch, builder, approver, *, minutes):
    from apps.placement import services

    with schema_context(tenant.schema_name):
        test = services.create_test(
            title="Placement", created_by=builder, branch=branch, time_limit_minutes=minutes
        )
        q = services.add_question(
            test=test, prompt="2+2?", question_type="single_choice", options=["3", "4"], correct_answer="4"
        )
        services.submit_for_review(test=test)
        test = services.approve_test(test=test, approver=approver)
    return test, q


def _setup(tenant, user_in, as_user, *, minutes):
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    teacher_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    hod_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    lead_u = user_in(tenant, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant.schema_name):
        lead = StudentProfileFactory.create(user=lead_u, branch=branch, status=StudentProfile.Status.LEAD)
    test, q = _approved_test(tenant, branch, teacher_u, hod_u, minutes=minutes)
    return {
        "branch": branch,
        "hod": as_user(tenant, hod_u),
        "lead": lead,
        "lead_c": as_user(tenant, lead_u),
        "test": test,
        "q": q,
    }


def _assign(s):
    r = s["hod"].post(ATTEMPTS, {"test": s["test"].id, "student": s["lead"].id}, format="json")
    assert r.status_code == 201, r.content
    return r.json()["data"]


def test_timed_attempt_gets_a_deadline(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user, minutes=30)
    body = _assign(s)
    assert body["expires_at"] is not None  # the lead sees their deadline


def test_untimed_attempt_has_no_deadline(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user, minutes=None)
    body = _assign(s)
    assert body["expires_at"] is None
    # ...and submitting any time works
    res = s["lead_c"].post(
        f"{ATTEMPTS}{body['id']}/submit/",
        {"answers": [{"question": s["q"].id, "response": "4"}]},
        format="json",
    )
    assert res.status_code == 200


def test_submit_before_deadline_succeeds(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user, minutes=30)
    aid = _assign(s)["id"]
    res = s["lead_c"].post(
        f"{ATTEMPTS}{aid}/submit/",
        {"answers": [{"question": s["q"].id, "response": "4"}]},
        format="json",
    )
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "graded"


def test_submit_after_deadline_is_rejected(tenant_a, user_in, as_user):
    from datetime import timedelta

    s = _setup(tenant_a, user_in, as_user, minutes=30)
    aid = _assign(s)["id"]
    # the clock runs out before the lead submits
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementAttempt

        PlacementAttempt.objects.filter(pk=aid).update(expires_at=timezone.now() - timedelta(minutes=1))
    res = s["lead_c"].post(
        f"{ATTEMPTS}{aid}/submit/",
        {"answers": [{"question": s["q"].id, "response": "4"}]},
        format="json",
    )
    assert res.status_code == 422
    assert res.json()["code"] == "attempt_expired"


def test_manager_sets_time_limit_on_the_test(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    teacher = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    created = teacher.post(
        TESTS, {"title": "T", "branch": branch.id, "time_limit_minutes": 20}, format="json"
    )
    assert created.status_code == 201
    assert created.json()["data"]["time_limit_minutes"] == 20
