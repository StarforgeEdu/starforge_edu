"""Assignments read selectors: role-scoped Assignment/Submission queries.

Drafts and other cohorts' assignments are filtered OUT of a student's queryset
(so they 404 on access, never a 403 that leaks existence)."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.assignments.models import Assignment, Submission
from core.permissions import Role

STAFF_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT}


def _cohorts_taught_by(user) -> QuerySet:
    from apps.cohorts.models import Cohort

    return (
        Cohort.objects.filter(
            Q(primary_teacher__user=user)
            | Q(co_teachers__teacher__user=user)
            | Q(lessons__teacher__user=user)
        )
        .values_list("id", flat=True)
        .distinct()
    )


def scoped_assignments(*, user, roles: set[str] | None = None) -> QuerySet[Assignment]:
    qs = Assignment.objects.select_related("cohort")
    if user.is_superuser:
        return qs
    if roles is None:
        roles = {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}
    if roles & STAFF_ROLES:
        return qs
    if Role.TEACHER in roles:  # own cohorts, incl. drafts
        return qs.filter(cohort_id__in=_cohorts_taught_by(user))
    if Role.STUDENT in roles:  # published only, own cohorts
        return qs.filter(
            status=Assignment.Status.PUBLISHED,
            cohort__memberships__student__user=user,
            cohort__memberships__end_date__isnull=True,
        ).distinct()
    return qs.none()


def scoped_submissions(*, user, roles: set[str] | None = None) -> QuerySet[Submission]:
    qs = Submission.objects.select_related("student__user", "assignment", "grade")
    if user.is_superuser:
        return qs
    if roles is None:
        roles = {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}
    if roles & STAFF_ROLES:
        return qs
    if Role.TEACHER in roles:  # submissions for cohorts they teach
        return qs.filter(assignment__cohort_id__in=_cohorts_taught_by(user))
    if Role.STUDENT in roles:  # own submissions only
        return qs.filter(student__user=user)
    return qs.none()
