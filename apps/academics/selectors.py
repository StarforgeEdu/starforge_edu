"""Academics read selectors: publication-gated, role-scoped Grade/Transcript
queries + honor-roll / academic-warning lists."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.academics.models import Exam, Grade, Transcript
from apps.org.selectors import get_center_settings
from core.permissions import Role

# Director / head-of-dept see everything (incl. unpublished drafts). TEACHER is
# scoped to cohorts they teach; student/parent are self/children AND gated to
# is_published=True (publication gating for parents — D2-C-7).
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


def _grade_base() -> QuerySet[Grade]:
    return Grade.objects.select_related("student__user", "subject", "term")


def scoped_grades(*, user, roles: set[str] | None = None) -> QuerySet[Grade]:
    qs = _grade_base()
    if user.is_superuser:
        return qs
    if roles is None:
        roles = {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}
    if roles & STAFF_ROLES:
        return qs
    if Role.TEACHER in roles:  # all grades (incl. drafts) for cohorts they teach
        return qs.filter(
            student__cohort_memberships__end_date__isnull=True,
            student__cohort_memberships__cohort_id__in=_cohorts_taught_by(user),
        ).distinct()
    if Role.PARENT in roles:  # published only, guardian-linked children
        return qs.filter(is_published=True, student__guardians__parent__user=user).distinct()
    if Role.STUDENT in roles:  # published only, self
        return qs.filter(is_published=True, student__user=user)
    return qs.none()


def scoped_transcripts(*, user, roles: set[str] | None = None) -> QuerySet[Transcript]:
    qs = Transcript.objects.select_related("student__user", "term")
    if user.is_superuser:
        return qs
    if roles is None:
        roles = {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}
    if roles & STAFF_ROLES:
        return qs
    if Role.PARENT in roles:
        return qs.filter(student__guardians__parent__user=user).distinct()
    if Role.STUDENT in roles:
        return qs.filter(student__user=user)
    return qs.none()


def scoped_exams(*, user, roles: set[str] | None = None) -> QuerySet[Exam]:
    """Exams visible/mutable for `user`. Superuser/staff (director, HoD) see all;
    a TEACHER is limited to exams of cohorts they teach (so the results /
    import-csv / publish write actions 404 on out-of-cohort exams via
    get_object()); everyone else sees none."""
    qs = Exam.objects.select_related("subject", "cohort", "term", "exam_type")
    if user.is_superuser:
        return qs
    if roles is None:
        roles = {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}
    if roles & STAFF_ROLES:
        return qs
    if Role.TEACHER in roles:
        return qs.filter(cohort_id__in=_cohorts_taught_by(user))
    return qs.none()


def _report_scope(qs: QuerySet[Grade], *, user, roles: set[str] | None) -> QuerySet[Grade]:
    """Scope a report queryset for consistency with grade scoping: superuser and
    staff (director / head-of-dept) stay tenant-wide; a TEACHER is limited to
    grades of students in cohorts they teach. `user=None` keeps tenant-wide
    behaviour (e.g. direct selector calls in tests / internal aggregates)."""
    if user is None or getattr(user, "is_superuser", False):
        return qs
    if roles is None:
        roles = {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}
    if roles & STAFF_ROLES:
        return qs
    if Role.TEACHER in roles:
        return qs.filter(
            student__cohort_memberships__end_date__isnull=True,
            student__cohort_memberships__cohort_id__in=_cohorts_taught_by(user),
        ).distinct()
    return qs


def honor_roll(*, term_id: int, settings=None, user=None, roles: set[str] | None = None) -> QuerySet[Grade]:
    """Published grades for the term at or above `honor_roll_min` (TD-13).
    Teacher-scoped to taught cohorts; director/HoD stay tenant-wide."""
    settings = settings or get_center_settings()
    qs = (
        _grade_base()
        .filter(term_id=term_id, is_published=True, value_raw__gte=settings.honor_roll_min)
        .order_by("-value_raw")
    )
    return _report_scope(qs, user=user, roles=roles)


def academic_warnings(
    *, term_id: int, settings=None, user=None, roles: set[str] | None = None
) -> QuerySet[Grade]:
    """Published grades for the term at or below `academic_warning_max` (TD-13).
    Teacher-scoped to taught cohorts; director/HoD stay tenant-wide."""
    settings = settings or get_center_settings()
    qs = (
        _grade_base()
        .filter(term_id=term_id, is_published=True, value_raw__lte=settings.academic_warning_max)
        .order_by("value_raw")
    )
    return _report_scope(qs, user=user, roles=roles)
