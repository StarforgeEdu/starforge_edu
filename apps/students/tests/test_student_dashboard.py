"""F4-1 — the signed-in student's own dashboard
(GET /api/v1/students/me/dashboard/). The student-surface mirror of the teacher
dashboard: their group, next lessons, open homework, recent grades, outstanding
balance, and outstanding rule acknowledgments."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.assignments.models import Assignment
from apps.cohorts.tests.factories import CohortFactory
from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_student_dashboard_returns_own_cockpit(tenant_a, user_in, as_user):
    branch = None
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch, name="Beginners A1", level="A1")
    # A real STUDENT-role user, then a profile in their group.
    user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        StudentProfileFactory.create(user=user, branch=branch, current_cohort=cohort)
        Assignment.objects.create(
            cohort=cohort,
            title="Essay 1",
            due_at=timezone.now() + timedelta(days=3),
            status=Assignment.Status.PUBLISHED,
        )

    client = as_user(tenant_a, user)
    resp = client.get("/api/v1/students/me/dashboard/")
    assert resp.status_code == 200, resp.content
    body = resp.json()["data"]
    assert body["group"] == "Beginners A1"
    assert body["level"] == "A1"
    assert body["open_homework_count"] == 1
    assert body["open_homework"][0]["title"] == "Essay 1"
    assert body["next_lessons"] == []
    assert body["recent_grades"] == []
    # outstanding balance is serialized as a string (Decimal), never null.
    assert isinstance(body["outstanding_uzs"], str)
    assert isinstance(body["pending_rule_acknowledgments"], int)


def test_dashboard_404_for_non_student(tenant_a, user_in, as_user):
    # A teacher has no StudentProfile -> not_a_student, not a 500.
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER]))
    resp = client.get("/api/v1/students/me/dashboard/")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_a_student"


def test_dashboard_requires_auth(tenant_a, client_for):
    resp = client_for(tenant_a).get("/api/v1/students/me/dashboard/")
    assert resp.status_code == 401
