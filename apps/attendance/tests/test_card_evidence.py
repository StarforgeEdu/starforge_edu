"""Production contract tests for per-lesson smart/warning card evidence."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.attendance.models import AttendanceRecord
from apps.attendance.services import mark_attendance
from apps.attendance.views.v1.attendance_views import MAX_MARK_ENTRIES, _mark_entries
from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
from apps.org.tests.factories import BranchFactory
from apps.schedule.models import Lesson
from apps.schedule.tests.factories import TermFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.teachers.tests.factories import TeacherProfileFactory
from core.exceptions import UnprocessableEntity, ValidationException

pytestmark = pytest.mark.django_db


def _lesson(*, branch, teacher):
    starts_at = timezone.now() - timedelta(minutes=30)
    return Lesson.objects.create(
        term=TermFactory(),
        cohort=CohortFactory(branch=branch, primary_teacher=teacher),
        teacher=teacher,
        title="English",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
    )


def test_card_evidence_round_trips_preserves_omission_and_supports_explicit_clear(tenant_a, user_in):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    teacher_user = user_in(tenant_a, roles=["teacher"], branch=branch)
    with schema_context(tenant_a.schema_name):
        teacher = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _lesson(branch=branch, teacher=teacher)
        student = StudentProfileFactory(branch=branch, current_cohort=lesson.cohort)
        CohortMembershipFactory(cohort=lesson.cohort, student=student)

        issued = mark_attendance(
            lesson=lesson,
            entries=[
                {
                    "student": student,
                    "status": AttendanceRecord.Status.PRESENT,
                    "card_type": AttendanceRecord.CardType.SMART,
                }
            ],
            actor=teacher_user,
        )["records"][0]
        assert issued.card_type == AttendanceRecord.CardType.SMART

        preserved = mark_attendance(
            lesson=lesson,
            entries=[{"student": student, "status": AttendanceRecord.Status.LATE}],
            actor=teacher_user,
        )["records"][0]
        assert preserved.card_type == AttendanceRecord.CardType.SMART

        cleared = mark_attendance(
            lesson=lesson,
            entries=[
                {
                    "student": student,
                    "status": AttendanceRecord.Status.PRESENT,
                    "card_type": AttendanceRecord.CardType.NONE,
                }
            ],
            actor=teacher_user,
        )["records"][0]
        assert cleared.card_type == AttendanceRecord.CardType.NONE


def test_card_evidence_api_is_closed_scoped_and_serialized(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    teacher_user = user_in(tenant_a, roles=["teacher"], branch=branch)
    with schema_context(tenant_a.schema_name):
        teacher = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _lesson(branch=branch, teacher=teacher)
        student = StudentProfileFactory(branch=branch, current_cohort=lesson.cohort)
        CohortMembershipFactory(cohort=lesson.cohort, student=student)
        lesson_id = lesson.pk
        student_id = student.pk

    client = as_user(tenant_a, teacher_user)
    response = client.post(
        f"/api/v1/attendance/lessons/{lesson_id}/mark/",
        [{"student": student_id, "status": "present", "card_type": "warning"}],
        format="json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["data"]["records"][0]["card_type"] == "warning"

    listing = client.get(f"/api/v1/attendance/records/?lesson={lesson_id}")
    assert listing.status_code == 200
    assert listing.json()["data"][0]["card_type"] == "warning"

    invalid = client.post(
        f"/api/v1/attendance/lessons/{lesson_id}/mark/",
        [{"student": student_id, "status": "present", "card_type": "gold"}],
        format="json",
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "validation_error"

    unknown = client.post(
        f"/api/v1/attendance/lessons/{lesson_id}/mark/",
        [{"student": student_id, "status": "present", "card_type": "smart", "award": True}],
        format="json",
    )
    assert unknown.status_code == 400
    with schema_context(tenant_a.schema_name):
        assert AttendanceRecord.objects.get(lesson_id=lesson_id).card_type == "warning"


def test_domain_rejects_unknown_card_type_before_database_write(tenant_a, user_in):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    teacher_user = user_in(tenant_a, roles=["teacher"], branch=branch)
    with schema_context(tenant_a.schema_name):
        teacher = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _lesson(branch=branch, teacher=teacher)
        student = StudentProfileFactory(branch=branch, current_cohort=lesson.cohort)
        CohortMembershipFactory(cohort=lesson.cohort, student=student)
        with pytest.raises(UnprocessableEntity) as exc:
            mark_attendance(
                lesson=lesson,
                entries=[{"student": student, "status": "present", "card_type": "gold"}],
                actor=teacher_user,
            )
        assert exc.value.code == "invalid_card_type"
        assert not AttendanceRecord.objects.filter(lesson=lesson).exists()


def test_domain_rejects_non_string_card_type_cleanly(tenant_a, user_in):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    teacher_user = user_in(tenant_a, roles=["teacher"], branch=branch)
    with schema_context(tenant_a.schema_name):
        teacher = TeacherProfileFactory(user=teacher_user, branch=branch)
        lesson = _lesson(branch=branch, teacher=teacher)
        student = StudentProfileFactory(branch=branch, current_cohort=lesson.cohort)
        CohortMembershipFactory(cohort=lesson.cohort, student=student)
        with pytest.raises(UnprocessableEntity) as exc:
            mark_attendance(
                lesson=lesson,
                entries=[{"student": student, "status": "present", "card_type": {}}],
                actor=teacher_user,
            )
        assert exc.value.code == "invalid_card_type"
        assert not AttendanceRecord.objects.filter(lesson=lesson).exists()


def test_attendance_batch_is_bounded_before_item_parsing():
    with pytest.raises(ValidationException) as exc:
        _mark_entries([None] * (MAX_MARK_ENTRIES + 1))
    assert exc.value.code == "validation_error"
