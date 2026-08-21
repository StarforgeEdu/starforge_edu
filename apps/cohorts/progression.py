"""Truthful, derived lesson-cycle progress for one cohort.

Only lessons explicitly recorded as ``completed`` advance a cycle.  A past
lesson that is still ``scheduled`` is reported as incomplete evidence instead
of being silently treated as taught.  The free-text cohort level is never
changed here: level promotion needs an authoritative level catalogue and an
explicit transition policy.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.cohorts.models import Cohort
from apps.schedule.models import Lesson

EXAM_REMINDER_WINDOW = dt.timedelta(days=7)


def completed_lessons_before(*, cohort_id: int, starts_at, lesson_id: int) -> int:
    """Count completed occurrences ordered before one candidate lesson."""

    return (
        Lesson.objects.filter(
            cohort_id=cohort_id,
            status=Lesson.Status.COMPLETED,
        )
        .filter(Q(starts_at__lt=starts_at) | Q(starts_at=starts_at, pk__lt=lesson_id))
        .count()
    )


def cycle_slot_after_completed(*, completed_count: int, cycle_length: int) -> int:
    """Return the one-based slot occupied by the next taught lesson."""

    return (completed_count % cycle_length) + 1


def lesson_cycle_signal(lesson: Lesson) -> dict[str, int | bool]:
    """Derive the cycle signal attached to a scheduled reminder."""

    completed_count = completed_lessons_before(
        cohort_id=lesson.cohort_id,
        starts_at=lesson.starts_at,
        lesson_id=lesson.pk,
    )
    cycle_length = int(lesson.cohort.lesson_cycle_length)
    slot = cycle_slot_after_completed(
        completed_count=completed_count,
        cycle_length=cycle_length,
    )
    return {
        "cycle_length": cycle_length,
        "cycle_lesson_number": slot,
        "is_cycle_exam_day": slot == cycle_length,
    }


def cohort_cycle_progress(cohort: Cohort, *, at=None) -> dict[str, Any]:
    """Build a client-ready cycle snapshot from explicit scheduling evidence."""

    now = at or timezone.now()
    lessons = Lesson.objects.filter(cohort=cohort)
    completed_count = lessons.filter(status=Lesson.Status.COMPLETED).count()
    cycle_length = int(cohort.lesson_cycle_length)
    completed_in_cycle = completed_count % cycle_length
    next_slot = cycle_slot_after_completed(
        completed_count=completed_count,
        cycle_length=cycle_length,
    )
    exam_day_due = next_slot == cycle_length
    past_uncompleted = lessons.filter(
        status=Lesson.Status.SCHEDULED,
        ends_at__lt=now,
    ).count()
    next_lesson = (
        lessons.filter(status=Lesson.Status.SCHEDULED, starts_at__gte=now)
        .select_related("room", "teacher")
        .order_by("starts_at", "pk")
        .first()
    )

    next_payload = None
    reminder_due = False
    if next_lesson is not None:
        next_payload = {
            "id": next_lesson.pk,
            "title": next_lesson.title,
            "starts_at": next_lesson.starts_at.isoformat(),
            "ends_at": next_lesson.ends_at.isoformat(),
            "room": next_lesson.room_id,
            "room_name": next_lesson.room.name if next_lesson.room else None,
            "teacher": next_lesson.teacher_id,
            "teacher_name": next_lesson.teacher.get_full_name(),
            "cycle_lesson_number": next_slot,
            "is_cycle_exam_day": exam_day_due,
        }
        reminder_due = exam_day_due and next_lesson.starts_at <= now + EXAM_REMINDER_WINDOW

    return {
        "cohort": cohort.pk,
        "current_level": cohort.level,
        "current_study_month": cohort.study_month,
        "lesson_cycle_length": cycle_length,
        "completed_lessons": completed_count,
        "completed_cycles": completed_count // cycle_length,
        "completed_in_current_cycle": completed_in_cycle,
        "next_cycle_lesson_number": next_slot,
        "lessons_remaining_in_cycle": cycle_length - completed_in_cycle,
        "exam_day_due": exam_day_due,
        "exam_reminder_due": reminder_due,
        "exam_reminder_window_days": EXAM_REMINDER_WINDOW.days,
        "next_scheduled_lesson": next_payload,
        "past_scheduled_lessons_without_completion": past_uncompleted,
        "completion_data_complete": past_uncompleted == 0,
        "level_progression_mode": "manual",
        "automatic_level_progression": False,
    }
