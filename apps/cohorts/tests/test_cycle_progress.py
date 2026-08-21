"""Lesson-cycle configuration, derivation, ownership, and reminder coverage."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db


def _lesson(*, cohort, term, teacher, starts_at, status):
    from apps.schedule.models import Lesson

    return Lesson.objects.create(
        term=term,
        cohort=cohort,
        teacher=teacher,
        title="English practice",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        status=status,
    )


def test_cycle_progress_is_truthful_and_never_promotes_free_text_level(tenant_a, as_role):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    client, _ = as_role(Role.DIRECTOR)
    now = timezone.now()
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory(level="Elementary", study_month=4, lesson_cycle_length=8)
        teacher = TeacherProfileFactory(branch=cohort.branch)
        term = TermFactory()
        for offset in range(7, 0, -1):
            _lesson(
                cohort=cohort,
                term=term,
                teacher=teacher,
                starts_at=now - timedelta(days=offset),
                status=Lesson.Status.COMPLETED,
            )
        _lesson(
            cohort=cohort,
            term=term,
            teacher=teacher,
            starts_at=now - timedelta(days=1, hours=2),
            status=Lesson.Status.SCHEDULED,
        )
        upcoming = _lesson(
            cohort=cohort,
            term=term,
            teacher=teacher,
            starts_at=now + timedelta(days=3),
            status=Lesson.Status.SCHEDULED,
        )

    response = client.get(f"/api/v1/cohorts/{cohort.pk}/cycle-progress/")

    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["lesson_cycle_length"] == 8
    assert data["completed_lessons"] == 7
    assert data["completed_cycles"] == 0
    assert data["completed_in_current_cycle"] == 7
    assert data["next_cycle_lesson_number"] == 8
    assert data["lessons_remaining_in_cycle"] == 1
    assert data["exam_day_due"] is True
    assert data["exam_reminder_due"] is True
    assert data["next_scheduled_lesson"]["id"] == upcoming.pk
    assert data["next_scheduled_lesson"]["is_cycle_exam_day"] is True
    assert data["past_scheduled_lessons_without_completion"] == 1
    assert data["completion_data_complete"] is False
    assert data["current_level"] == "Elementary"
    assert data["current_study_month"] == 4
    assert data["level_progression_mode"] == "manual"
    assert data["automatic_level_progression"] is False
    with schema_context(tenant_a.schema_name):
        cohort.refresh_from_db()
        assert cohort.level == "Elementary"


def test_cycle_configuration_accepts_only_eight_or_twelve(tenant_a, as_role):
    from apps.cohorts.tests.factories import CohortFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory(lesson_cycle_length=12)

    accepted = client.patch(
        f"/api/v1/cohorts/{cohort.pk}/",
        {"lesson_cycle_length": 8},
        format="json",
    )
    rejected = client.patch(
        f"/api/v1/cohorts/{cohort.pk}/",
        {"lesson_cycle_length": 9},
        format="json",
    )

    assert accepted.status_code == 200, accepted.content
    assert accepted.json()["data"]["lesson_cycle_length"] == 8
    assert rejected.status_code == 400
    assert "lesson_cycle_length" in rejected.json()["errors"]
    with schema_context(tenant_a.schema_name), pytest.raises(IntegrityError), transaction.atomic():
        type(cohort).objects.filter(pk=cohort.pk).update(lesson_cycle_length=9)
    with schema_context(tenant_a.schema_name), pytest.raises(IntegrityError), transaction.atomic():
        type(cohort).objects.filter(pk=cohort.pk).update(study_month=0)


def test_primary_teacher_can_update_only_bounded_teaching_progress(
    tenant_a,
    user_in,
    client_for,
):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.teachers.models import TeacherProfile
    from apps.teachers.tests.factories import TeacherProfileFactory
    from core.session_auth import create_session

    primary_user = user_in(tenant_a, roles=[Role.TEACHER])
    with schema_context(tenant_a.schema_name):
        branch = primary_user.role_memberships.get().branch
        primary = TeacherProfile.objects.filter(user=primary_user).first()
        if primary is None:
            primary = TeacherProfileFactory(user=primary_user, branch=branch)
    other_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    with schema_context(tenant_a.schema_name):
        other = TeacherProfile.objects.filter(user=other_user).first()
        if other is None:
            other = TeacherProfileFactory(user=other_user, branch=branch)
        cohort = CohortFactory(
            branch=branch,
            primary_teacher=primary,
            level="Elementary",
            study_month=2,
            lesson_cycle_length=12,
        )
        primary_session = create_session(
            primary_user,
            principal_kind="teacher",
            principal_id=primary.pk,
        )
        other_session = create_session(
            other_user,
            principal_kind="teacher",
            principal_id=other.pk,
        )

    primary_client = client_for(tenant_a)
    primary_client.credentials(HTTP_AUTHORIZATION=f"Bearer {primary_session.key}")
    updated = primary_client.patch(
        f"/api/v1/cohorts/{cohort.pk}/teaching-progress/",
        {
            "level": "Pre-intermediate",
            "study_month": 3,
            "lesson_cycle_length": 8,
        },
        format="json",
    )
    unsupported = primary_client.patch(
        f"/api/v1/cohorts/{cohort.pk}/teaching-progress/",
        {"name": "Takeover"},
        format="json",
    )

    other_client = client_for(tenant_a)
    other_client.credentials(HTTP_AUTHORIZATION=f"Bearer {other_session.key}")
    denied = other_client.patch(
        f"/api/v1/cohorts/{cohort.pk}/teaching-progress/",
        {"study_month": 9},
        format="json",
    )

    assert updated.status_code == 200, updated.content
    assert updated.json()["data"] | {"updated_at": "ignored"} == {
        "cohort": cohort.pk,
        "level": "Pre-intermediate",
        "study_month": 3,
        "lesson_cycle_length": 8,
        "updated_at": "ignored",
    }
    assert unsupported.status_code == 400
    assert set(unsupported.json()["errors"]) == {"name"}
    assert denied.status_code == 403
    assert denied.json()["code"] == "out_of_scope"
    with schema_context(tenant_a.schema_name):
        from apps.audit.models import AuditLog

        cohort.refresh_from_db()
        assert cohort.level == "Pre-intermediate"
        assert cohort.study_month == 3
        assert cohort.lesson_cycle_length == 8
        audit = AuditLog.objects.get(
            resource_type="cohorts.Cohort",
            resource_id=str(cohort.pk),
            action="update",
        )
        assert audit.actor_principal_kind == "teacher"
        assert audit.actor_principal_id == primary.pk
        assert audit.scope_status == "scoped"
        assert audit.scope_branch_id == branch.pk
        assert audit.after["study_month"] == 3


def test_exact_teacher_principal_reads_only_taught_cohorts(tenant_a, user_in, client_for):
    from apps.cohorts.models import CohortTeacher
    from apps.cohorts.selectors import taught_cohorts
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.models import TeacherProfile, TeacherType
    from apps.teachers.tests.factories import TeacherProfileFactory
    from core.session_auth import create_session

    user = user_in(tenant_a, roles=[Role.TEACHER])
    with schema_context(tenant_a.schema_name):
        membership_branch = user.role_memberships.get().branch
        teacher = TeacherProfile.objects.filter(user=user).first()
        if teacher is None:
            teacher = TeacherProfileFactory(user=user, branch=membership_branch)
        own = CohortFactory(branch=membership_branch, primary_teacher=teacher)
        additional = CohortFactory(branch=membership_branch)
        CohortTeacher.objects.create(
            cohort=additional,
            teacher=teacher,
            teacher_type=TeacherType.objects.get(slug="co-teacher"),
        )
        other = CohortFactory(branch=membership_branch)
        Lesson.objects.create(
            term=TermFactory(),
            cohort=other,
            teacher=teacher,
            title="One-off cover lesson",
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        session = create_session(user, principal_kind="teacher", principal_id=teacher.pk)
        assert set(
            taught_cohorts(
                teacher_id=teacher.pk,
                include_lesson_teacher=False,
            ).values_list("pk", flat=True)
        ) == {own.pk, additional.pk}
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")

    listed = client.get("/api/v1/cohorts/")
    own_detail = client.get(f"/api/v1/cohorts/{own.pk}/")
    other_detail = client.get(f"/api/v1/cohorts/{other.pk}/")

    assert listed.status_code == 200, listed.content
    assert {row["id"] for row in listed.json()["data"]} == {own.pk, additional.pk}
    assert own_detail.status_code == 200
    assert other_detail.status_code == 403
    assert other_detail.json()["code"] == "out_of_scope"


def test_due_final_cycle_lesson_notifies_exact_teacher_principal(tenant_a):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.notifications.models import EventType, Notification
    from apps.schedule.models import Lesson
    from apps.schedule.services import emit_due_reminders
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    now = timezone.now()
    with schema_context(tenant_a.schema_name):
        teacher = TeacherProfileFactory(user__preferred_language="en")
        cohort = CohortFactory(branch=teacher.branch, primary_teacher=teacher, lesson_cycle_length=8)
        term = TermFactory()
        for offset in range(7, 0, -1):
            _lesson(
                cohort=cohort,
                term=term,
                teacher=teacher,
                starts_at=now - timedelta(days=offset),
                status=Lesson.Status.COMPLETED,
            )
        due = _lesson(
            cohort=cohort,
            term=term,
            teacher=teacher,
            starts_at=now + timedelta(minutes=30),
            status=Lesson.Status.SCHEDULED,
        )

        assert emit_due_reminders() == 1
        notification = Notification.objects.get(
            recipient_principal_kind="teacher",
            recipient_principal_id=teacher.pk,
            event_type=EventType.SCHEDULE_CYCLE_EXAM_REMINDER,
        )
        assert notification.user_id == teacher.user_id
        assert notification.data == {
            "lesson_id": due.pk,
            "cohort_id": cohort.pk,
            "lesson_title": due.title,
            "starts_at": due.starts_at.isoformat(),
            "cycle_lesson_number": 8,
            "cycle_length": 8,
            "is_cycle_exam_day": True,
            "kind": "cycle_exam",
        }
        assert notification.title == "Exam lesson coming up"
        assert "final exam lesson" in notification.body


def test_cycle_progress_openapi_is_closed_and_authenticated():
    from core.openapi import build_schema

    operation = build_schema(None)["paths"]["/api/v1/cohorts/{pk}/cycle-progress/"]["get"]
    assert operation["security"]
    assert "Requires permission `cohorts:read`." in operation["description"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False
    data = schema["properties"]["data"]
    assert data["additionalProperties"] is False
    assert data["properties"]["lesson_cycle_length"]["enum"] == [8, 12]
    assert data["properties"]["current_study_month"]["maximum"] == 600

    update = build_schema(None)["paths"]["/api/v1/cohorts/{pk}/teaching-progress/"]["patch"]
    assert update["security"]
    assert "Requires permission `academics:write`." in update["description"]
    request = update["requestBody"]["content"]["application/json"]["schema"]
    assert request["additionalProperties"] is False
    assert request["properties"]["lesson_cycle_length"]["enum"] == [8, 12]
    assert request["properties"]["study_month"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 600,
    }
