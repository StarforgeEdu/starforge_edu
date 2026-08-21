"""Tenant teacher types and canonical multi-teacher cohort assignments."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.cohorts.models import CohortTeacher
from apps.cohorts.selectors import taught_cohorts
from apps.cohorts.tests.factories import CohortFactory
from apps.org.tests.factories import BranchFactory, DepartmentFactory
from apps.teachers.models import TeacherType
from apps.teachers.tests.factories import TeacherProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_cohort_admin_uses_typed_assignments_as_the_only_teacher_editor():
    from django.contrib import admin

    from apps.cohorts.models import Cohort

    model_admin = admin.site._registry[Cohort]
    assert "primary_teacher" in model_admin.exclude
    assert "primary_teacher" not in model_admin.autocomplete_fields


def test_old_node_role_writes_and_new_typed_writes_are_dual_compatible(tenant_a):
    """The expand migration supports old images during rollout and rollback."""
    from django.db import connection

    from apps.teachers.tests.factories import TeacherTypeFactory

    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory()
        old_teacher = TeacherProfileFactory(branch=cohort.branch)
        typed_teacher = TeacherProfileFactory(branch=cohort.branch)
        with connection.cursor() as cursor:
            # SQL shape emitted by the pre-release model: no teacher_type column.
            cursor.execute(
                """
                INSERT INTO cohorts_cohortteacher (cohort_id, teacher_id, role)
                VALUES (%s, %s, %s)
                RETURNING id, teacher_type_id, role
                """,
                [cohort.id, old_teacher.id, "assistant"],
            )
            legacy_id, assistant_type_id, legacy_role = cursor.fetchone()
        assert assistant_type_id == TeacherType.objects.get(slug="assistant").id
        assert legacy_role == "assistant"

        # An old-node role edit updates the canonical FK as well.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE cohorts_cohortteacher
                SET role = %s
                WHERE id = %s
                RETURNING teacher_type_id, role
                """,
                ["co_teacher", legacy_id],
            )
            co_teacher_type_id, legacy_role = cursor.fetchone()
        assert co_teacher_type_id == TeacherType.objects.get(slug="co-teacher").id
        assert legacy_role == "co_teacher"

        custom_type = TeacherTypeFactory(name="Workshop Mentor", slug="workshop-mentor")
        typed = CohortTeacher.objects.create(
            cohort=cohort,
            teacher=typed_teacher,
            teacher_type=custom_type,
        )
        typed.refresh_from_db()
        assert typed.role == "co_teacher"


TYPES_URL = "/api/v1/cohorts/teacher-types/"


@pytest.fixture
def director(as_role):
    return as_role(Role.DIRECTOR)[0]


def _type(tenant, slug: str) -> TeacherType:
    with schema_context(tenant.schema_name):
        return TeacherType.objects.get(slug=slug)


def test_system_teacher_types_are_seeded_and_ordered(director, tenant_a):
    response = director.get(TYPES_URL)
    assert response.status_code == 200
    rows = response.json()["data"]
    assert [row["slug"] for row in rows[:4]] == [
        "main-teacher",
        "video-teacher",
        "assistant",
        "co-teacher",
    ]
    assert rows[0]["is_default"] is True
    assert all(row["is_system"] for row in rows[:4])


def test_custom_teacher_type_crud_and_case_insensitive_duplicate(director, tenant_a):
    created = director.post(
        TYPES_URL,
        {
            "name": "Speaking Coach",
            "description": "Conversation practice",
            "sort_order": 55,
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    row = created.json()["data"]
    assert row["slug"] == "speaking-coach"
    assert row["is_system"] is False

    duplicate = director.post(
        TYPES_URL,
        {"name": "sPeAkInG cOaCh", "slug": "another-slug"},
        format="json",
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "teacher_type_exists"
    duplicate_slug = director.post(
        TYPES_URL,
        {"name": "Different name", "slug": "SPEAKING-COACH"},
        format="json",
    )
    assert duplicate_slug.status_code == 409
    assert "slug" in duplicate_slug.json()["errors"]

    updated = director.patch(
        f"{TYPES_URL}{row['id']}/",
        {"description": "Updated", "sort_order": 56},
        format="json",
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["description"] == "Updated"
    assert director.delete(f"{TYPES_URL}{row['id']}/").status_code == 204


def test_system_types_cannot_be_deactivated_or_deleted(director, tenant_a):
    assistant = _type(tenant_a, "assistant")
    deactivated = director.patch(f"{TYPES_URL}{assistant.id}/", {"is_active": False}, format="json")
    assert deactivated.status_code == 400
    assert deactivated.json()["code"] == "system_teacher_type_immutable"
    deleted = director.delete(f"{TYPES_URL}{assistant.id}/")
    assert deleted.status_code == 400
    assert deleted.json()["code"] == "system_teacher_type_immutable"


def test_inactive_custom_type_cannot_be_newly_assigned(director, tenant_a):
    custom = director.post(TYPES_URL, {"name": "Observer", "slug": "observer"}, format="json").json()["data"]
    assert (
        director.patch(f"{TYPES_URL}{custom['id']}/", {"is_active": False}, format="json").status_code == 200
    )
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        teacher = TeacherProfileFactory(branch=branch)
    response = director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/",
        {"teacher": teacher.id, "teacher_type": custom["id"]},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["code"] == "inactive_teacher_type"


def test_duplicate_is_idempotent_but_teacher_can_hold_multiple_types_and_assistants(director, tenant_a):
    assistant = _type(tenant_a, "assistant")
    co_teacher = _type(tenant_a, "co-teacher")
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        first_teacher = TeacherProfileFactory(branch=branch)
        second_teacher = TeacherProfileFactory(branch=branch)

    url = f"/api/v1/cohorts/{cohort.id}/teachers/"
    first = director.post(
        url,
        {"teacher": first_teacher.id, "teacher_type": assistant.id},
        format="json",
    )
    replay = director.post(
        url,
        {"teacher": first_teacher.id, "teacher_type": assistant.id},
        format="json",
    )
    second_type = director.post(
        url,
        {"teacher": first_teacher.id, "teacher_type": co_teacher.id},
        format="json",
    )
    second_assistant = director.post(
        url,
        {"teacher": second_teacher.id, "teacher_type": assistant.id},
        format="json",
    )
    assert [response.status_code for response in (first, replay, second_type, second_assistant)] == [
        201,
        200,
        201,
        201,
    ]
    with schema_context(tenant_a.schema_name):
        assert CohortTeacher.objects.filter(cohort=cohort).count() == 3
        assert CohortTeacher.objects.filter(cohort=cohort, teacher_type=assistant).count() == 2


def test_assignment_update_collision_and_exact_delete(director, tenant_a):
    assistant = _type(tenant_a, "assistant")
    co_teacher = _type(tenant_a, "co-teacher")
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        teacher = TeacherProfileFactory(branch=branch)
    url = f"/api/v1/cohorts/{cohort.id}/teachers/"
    assistant_row = director.post(
        url, {"teacher": teacher.id, "teacher_type": assistant.id}, format="json"
    ).json()["data"]
    co_teacher_row = director.post(
        url, {"teacher": teacher.id, "teacher_type": co_teacher.id}, format="json"
    ).json()["data"]

    collision = director.patch(
        f"{url}{co_teacher_row['id']}/",
        {"teacher_type": assistant.id},
        format="json",
    )
    assert collision.status_code == 409
    assert collision.json()["code"] == "teacher_assignment_exists"
    assert director.delete(f"{url}{assistant_row['id']}/").status_code == 204
    with schema_context(tenant_a.schema_name):
        remaining = CohortTeacher.objects.get(pk=co_teacher_row["id"])
        assert remaining.teacher_type_id == co_teacher.id


def test_assignment_requires_matching_branch_and_department(director, tenant_a):
    assistant = _type(tenant_a, "assistant")
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort_department = DepartmentFactory(branch=branch)
        other_department = DepartmentFactory(branch=branch)
        cohort = CohortFactory(branch=branch, department=cohort_department)
        teacher = TeacherProfileFactory(branch=branch, department=other_department)
    response = director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/",
        {"teacher": teacher.id, "teacher_type": assistant.id},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["code"] == "cross_department_relationship"


def test_assignment_detail_is_bound_to_its_cohort(director, tenant_a):
    assistant = _type(tenant_a, "assistant")
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort_a = CohortFactory(branch=branch, name="A")
        cohort_b = CohortFactory(branch=branch, name="B")
        teacher = TeacherProfileFactory(branch=branch)
    assignment = director.post(
        f"/api/v1/cohorts/{cohort_a.id}/teachers/",
        {"teacher": teacher.id, "teacher_type": assistant.id},
        format="json",
    ).json()["data"]
    response = director.get(f"/api/v1/cohorts/{cohort_b.id}/teachers/{assignment['id']}/")
    assert response.status_code == 404


def test_assignment_actions_keep_branch_idor_guard(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        home = BranchFactory()
        other = BranchFactory()
        cohort = CohortFactory(branch=other)
        teacher = TeacherProfileFactory(branch=other)
        assistant = TeacherType.objects.get(slug="assistant")
        assignment = CohortTeacher.objects.create(cohort=cohort, teacher=teacher, teacher_type=assistant)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=home))
    assert client.get(f"/api/v1/cohorts/{cohort.id}/teachers/{assignment.id}/").status_code == 403
    assert client.delete(f"/api/v1/cohorts/{cohort.id}/teachers/{assignment.id}/").status_code == 403


def test_main_assignments_project_legacy_primary_and_reproject_on_delete(director, tenant_a):
    main = _type(tenant_a, "main-teacher")
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        first = TeacherProfileFactory(branch=branch)
        second = TeacherProfileFactory(branch=branch)
    url = f"/api/v1/cohorts/{cohort.id}/teachers/"
    first_row = director.post(url, {"teacher": first.id, "teacher_type": main.id}, format="json").json()[
        "data"
    ]
    director.post(url, {"teacher": second.id, "teacher_type": main.id}, format="json")
    with schema_context(tenant_a.schema_name):
        cohort.refresh_from_db()
        assert cohort.primary_teacher_id == first.id

    assert director.delete(f"{url}{first_row['id']}/").status_code == 204
    detail = director.get(f"/api/v1/cohorts/{cohort.id}/").json()["data"]
    assert detail["primary_teacher"] == second.id


def test_legacy_primary_edit_creates_canonical_main_assignment(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher = TeacherProfileFactory(branch=branch)
    created = director.post(
        "/api/v1/cohorts/",
        {
            "name": "Canonical",
            "branch": branch.id,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "audience_type": "adults",
            "primary_teacher": teacher.id,
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    cohort_id = created.json()["data"]["id"]
    with schema_context(tenant_a.schema_name):
        assert CohortTeacher.objects.filter(
            cohort_id=cohort_id,
            teacher=teacher,
            teacher_type__slug="main-teacher",
        ).exists()


def test_custom_assignment_is_canonical_for_teacher_cohort_selectors(director, tenant_a):
    custom = director.post(TYPES_URL, {"name": "Lab Teacher", "slug": "lab-teacher"}, format="json").json()[
        "data"
    ]
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        teacher = TeacherProfileFactory(branch=branch)
    director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/",
        {"teacher": teacher.id, "teacher_type": custom["id"]},
        format="json",
    )
    with schema_context(tenant_a.schema_name):
        assert taught_cohorts(teacher=teacher).filter(pk=cohort.id).exists()


def test_type_in_use_is_protected(director, tenant_a):
    custom = director.post(TYPES_URL, {"name": "Workshop", "slug": "workshop"}, format="json").json()["data"]
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        teacher = TeacherProfileFactory(branch=branch)
    director.post(
        f"/api/v1/cohorts/{cohort.id}/teachers/",
        {"teacher": teacher.id, "teacher_type": custom["id"]},
        format="json",
    )
    response = director.delete(f"{TYPES_URL}{custom['id']}/")
    assert response.status_code == 409
    assert response.json()["code"] == "teacher_type_in_use"


def test_teacher_can_read_types_but_cannot_manage_them(tenant_a, as_role):
    client, _ = as_role(Role.TEACHER)
    assert client.get(TYPES_URL).status_code == 200
    assert (
        client.post(TYPES_URL, {"name": "Forbidden", "slug": "forbidden"}, format="json").status_code == 403
    )
