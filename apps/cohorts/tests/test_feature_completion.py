"""Feature-completion endpoints (audit gaps):
- F2 "remove from group": unenroll a student to groupless without moving them.
- F4 co-teacher/assistant roster write path (assign / re-assign / unassign).
"""

from __future__ import annotations

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.cohorts.models import CohortMembership, CohortTeacher
from apps.cohorts.services import enroll_student_in_cohort
from apps.cohorts.tests.factories import CohortFactory
from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.teachers.tests.factories import TeacherProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db


@pytest.fixture
def director(as_role):
    return as_role(Role.DIRECTOR)[0]


# --- F2: remove from group (unenroll to groupless) -------------------------
def test_remove_student_ends_membership_and_clears_primary(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
        student = StudentProfileFactory.create(branch=branch)
        membership = enroll_student_in_cohort(cohort=cohort, student=student)
        student.refresh_from_db()
        assert student.current_cohort_id == cohort.id  # primary set on first enroll

    resp = director.post(
        f"/api/v1/cohorts/{cohort.id}/remove-student/",
        {"student": student.id, "reason": "left_group"},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["data"]["end_date"] == timezone.now().date().isoformat()

    with schema_context(tenant_a.schema_name):
        membership.refresh_from_db()
        assert membership.end_date == timezone.now().date()  # end-dated, not deleted
        assert membership.moved_reason == "left_group"
        assert CohortMembership.objects.filter(student=student).count() == 1  # history kept
        student.refresh_from_db()
        assert student.current_cohort_id is None  # groupless now


def test_remove_student_recomputes_primary_from_remaining_membership(
    director, tenant_a, django_capture_on_commit_callbacks
):
    """Removing the PRIMARY cohort when the student still has another active membership
    re-points current_cohort at the survivor (stays truthful, never left dangling)."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort_a = CohortFactory.create(branch=branch, name="A")
        cohort_b = CohortFactory.create(branch=branch, name="B")
        student = StudentProfileFactory.create(branch=branch)
        with django_capture_on_commit_callbacks(execute=True):
            enroll_student_in_cohort(cohort=cohort_a, student=student)  # primary = A
        with django_capture_on_commit_callbacks(execute=True):
            enroll_student_in_cohort(cohort=cohort_b, student=student)  # secondary

    resp = director.post(
        f"/api/v1/cohorts/{cohort_a.id}/remove-student/", {"student": student.id}, format="json"
    )
    assert resp.status_code == 200

    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        assert student.current_cohort_id == cohort_b.id  # survivor becomes primary
        active = CohortMembership.objects.filter(student=student, end_date__isnull=True)
        assert list(active.values_list("cohort_id", flat=True)) == [cohort_b.id]


def test_remove_student_not_enrolled_400(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
        student = StudentProfileFactory.create(branch=branch)  # never enrolled

    resp = director.post(
        f"/api/v1/cohorts/{cohort.id}/remove-student/", {"student": student.id}, format="json"
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "not_enrolled"


def test_remove_student_get_405(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create()
    assert director.get(f"/api/v1/cohorts/{cohort.id}/remove-student/").status_code == 405


# --- F4: co-teacher / assistant roster -------------------------------------
def test_assign_and_list_co_teacher(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
        teacher = TeacherProfileFactory.create(branch=branch)

    resp = director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/",
        {"teacher": teacher.id, "role": "assistant"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["data"]["teacher"] == teacher.id
    assert resp.json()["data"]["role"] == "assistant"

    roster = director.get(f"/api/v1/cohorts/{cohort.id}/teachers/")
    assert roster.status_code == 200
    assert roster.json()["data"] == [{"id": resp.json()["data"]["id"], "teacher": teacher.id, "role": "assistant"}]

    # And it surfaces on the cohort detail's co_teachers block.
    detail = director.get(f"/api/v1/cohorts/{cohort.id}/")
    assert detail.json()["data"]["co_teachers"][0]["teacher"] == teacher.id


def test_reassign_co_teacher_updates_role_idempotently(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
        teacher = TeacherProfileFactory.create(branch=branch)

    first = director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/",
        {"teacher": teacher.id, "role": "co_teacher"},
        format="json",
    )
    assert first.status_code == 201
    # Re-assign the SAME teacher -> upsert (200, role updated, no duplicate row, no 409).
    second = director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/",
        {"teacher": teacher.id, "role": "assistant"},
        format="json",
    )
    assert second.status_code == 200
    assert second.json()["data"]["role"] == "assistant"
    with schema_context(tenant_a.schema_name):
        assert CohortTeacher.objects.filter(cohort=cohort, teacher=teacher).count() == 1


def test_assign_co_teacher_invalid_role_400(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
        teacher = TeacherProfileFactory.create(branch=branch)
    resp = director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/",
        {"teacher": teacher.id, "role": "headmaster"},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_assign_co_teacher_unknown_teacher_400(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create()
    resp = director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/",
        {"teacher": 9999999, "role": "co_teacher"},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_teacher"


def test_remove_co_teacher(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
        teacher = TeacherProfileFactory.create(branch=branch)
        CohortTeacher.objects.create(cohort=cohort, teacher=teacher, role="co_teacher")

    resp = director.delete(f"/api/v1/cohorts/{cohort.id}/teachers/{teacher.id}/")
    assert resp.status_code == 204
    with schema_context(tenant_a.schema_name):
        assert not CohortTeacher.objects.filter(cohort=cohort, teacher=teacher).exists()


def test_remove_unassigned_co_teacher_404(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
        teacher = TeacherProfileFactory.create(branch=branch)  # never assigned
    resp = director.delete(f"/api/v1/cohorts/{cohort.id}/teachers/{teacher.id}/")
    assert resp.status_code == 404


def test_co_teacher_default_role_is_co_teacher(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
        teacher = TeacherProfileFactory.create(branch=branch)
    resp = director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/", {"teacher": teacher.id}, format="json"
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["role"] == "co_teacher"


# --- branch scoping (the new actions inherit _get_in_scope) -----------------
def test_new_actions_are_branch_scoped(tenant_a, user_in, as_user):
    """A branch-scoped REGISTRAR (has cohorts:* but only for its own branch) is 403'd on
    the new remove-student / teachers actions for an out-of-branch cohort, and no mutation
    lands — same object-scope guard as enroll/move."""
    with schema_context(tenant_a.schema_name):
        home = BranchFactory.create()
        other = BranchFactory.create()
        cohort = CohortFactory.create(branch=other)
        student = StudentProfileFactory.create(branch=other)
        teacher = TeacherProfileFactory.create(branch=other)
        enroll_student_in_cohort(cohort=cohort, student=student)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=home))

    remove = client.post(
        f"/api/v1/cohorts/{cohort.id}/remove-student/", {"student": student.id}, format="json"
    )
    assign = client.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/", {"teacher": teacher.id}, format="json"
    )
    assert remove.status_code == 403
    assert assign.status_code == 403
    with schema_context(tenant_a.schema_name):
        # nothing mutated across the branch boundary
        assert CohortMembership.objects.filter(
            student=student, end_date__isnull=True
        ).exists()
        assert not CohortTeacher.objects.filter(cohort=cohort).exists()
