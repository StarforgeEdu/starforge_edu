"""F3-2 — teacher dashboard aggregate."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/teachers/dashboard/"


def test_teacher_dashboard_aggregates(tenant_a, as_user):
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher = TeacherProfileFactory(branch=branch)
        RoleMembership.objects.create(user=teacher.user, branch=branch, role=Role.TEACHER)
        teacher.user.refresh_from_db()
        cohort = CohortFactory(branch=branch, primary_teacher=teacher, level="A1")
        student = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=cohort, student=student)

    body = as_user(tenant_a, teacher.user).get(URL).json()["data"]
    assert body["groups_count"] == 1
    assert body["students_count"] == 1
    assert body["level_groups"] == {"A1": 1}
    assert "next_lessons" in body
    assert "upcoming_exams" in body


def test_dashboard_404_for_non_teacher(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)  # a director has no teacher profile
    resp = director.get(URL)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_a_teacher"  # layered view -> success/error envelope
