"""Shared cohort ownership selectors.

Typed ``CohortTeacher`` assignments are canonical.  The scalar primary-teacher FK is
kept as a transitional fallback for rows created by old clients or fixtures, while a
scheduled lesson remains evidence that the lesson's teacher teaches that cohort.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.cohorts.models import Cohort


def taught_cohorts(
    *,
    teacher=None,
    teacher_id: int | None = None,
    user=None,
    user_id: int | None = None,
    include_lesson_teacher: bool = True,
) -> QuerySet[Cohort]:
    supplied = sum(value is not None for value in (teacher, teacher_id, user, user_id))
    if supplied != 1:
        raise ValueError("Provide exactly one of teacher, teacher_id, user, or user_id.")

    if teacher is not None:
        visible = Q(co_teachers__teacher=teacher) | Q(primary_teacher=teacher)
        if include_lesson_teacher:
            visible |= Q(lessons__teacher=teacher)
    elif teacher_id is not None:
        visible = Q(co_teachers__teacher_id=teacher_id) | Q(primary_teacher_id=teacher_id)
        if include_lesson_teacher:
            visible |= Q(lessons__teacher_id=teacher_id)
    elif user is not None:
        visible = Q(co_teachers__teacher__user=user) | Q(primary_teacher__user=user)
        if include_lesson_teacher:
            visible |= Q(lessons__teacher__user=user)
    else:
        visible = Q(co_teachers__teacher__user_id=user_id) | Q(primary_teacher__user_id=user_id)
        if include_lesson_teacher:
            visible |= Q(lessons__teacher__user_id=user_id)
    return Cohort.objects.filter(visible).distinct()
