"""F3-1 — dynamic lesson types: manager CRUD + materialized lessons inherit
the rule's lesson_type."""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.cohorts.tests.factories import CohortFactory
from apps.org.tests.factories import BranchFactory, RoomFactory
from apps.schedule import services
from apps.schedule.models import LessonType
from apps.schedule.tests.factories import TermFactory
from apps.teachers.tests.factories import TeacherProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/schedule/lesson-types/"


def test_manager_creates_type_teacher_cannot(as_role):
    director, _ = as_role(Role.DIRECTOR)
    resp = director.post(URL, {"name": "Speaking Lesson", "color": "#3b82f6"}, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["data"]["slug"] == "speaking-lesson"  # auto-slugged

    teacher, _ = as_role(Role.TEACHER)
    assert teacher.get(URL).status_code == 200  # schedule:read can list
    assert teacher.post(URL, {"name": "X"}, format="json").status_code == 403  # no schedule:write


def test_materialized_lessons_inherit_rule_lesson_type(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        lt = LessonType.objects.create(name="Main Lesson", slug="main-lesson")
        # Anchor to the next future Monday: materialize_rule only creates FUTURE
        # occurrences, so a hardcoded calendar window silently stops producing
        # lessons once it passes (a rotting `assert lessons`).
        today = timezone.localdate()
        anchor = today + timedelta(days=((0 - today.weekday()) % 7 or 7))
        rule = services.create_rule(
            term=TermFactory(start_date=anchor - timedelta(days=180), end_date=anchor + timedelta(days=180)),
            cohort=CohortFactory(branch=branch),
            teacher=TeacherProfileFactory(branch=branch),
            room=RoomFactory(branch=branch),
            lesson_type=lt,
            title="Algebra",
            rrule="FREQ=WEEKLY;BYDAY=MO",
            start_date=anchor,
            end_date=anchor + timedelta(days=21),
            start_time=time(14, 0),
            end_time=time(15, 30),
        )
        lessons: list[Any] = list(rule.lessons.all())
        assert lessons
        assert all(lesson.lesson_type_id == lt.id for lesson in lessons)
