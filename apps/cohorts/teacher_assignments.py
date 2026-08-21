"""Canonical typed teacher-assignment helpers.

``Cohort.primary_teacher`` remains a temporary compatibility projection.  All new
relationship logic is backed by :class:`CohortTeacher`; these helpers keep the scalar
field synchronized for older API clients and admin workflows.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.cohorts.models import Cohort, CohortTeacher
from apps.teachers.models import TeacherType

MAIN_TEACHER_SLUG = "main-teacher"
VIDEO_TEACHER_SLUG = "video-teacher"
ASSISTANT_SLUG = "assistant"
CO_TEACHER_SLUG = "co-teacher"

LEGACY_ROLE_TO_SLUG = {
    "main_teacher": MAIN_TEACHER_SLUG,
    "main-teacher": MAIN_TEACHER_SLUG,
    "video_teacher": VIDEO_TEACHER_SLUG,
    "video-teacher": VIDEO_TEACHER_SLUG,
    "assistant": ASSISTANT_SLUG,
    "co_teacher": CO_TEACHER_SLUG,
    "co-teacher": CO_TEACHER_SLUG,
}


def legacy_role_for_type(teacher_type: TeacherType) -> str:
    """Stable legacy ``role`` output retained while clients adopt ``teacher_type``."""
    return teacher_type.slug.replace("-", "_")


def type_slug_for_legacy_role(role: str) -> str | None:
    return LEGACY_ROLE_TO_SLUG.get(str(role).strip().lower())


def main_teacher_type() -> TeacherType | None:
    return TeacherType.objects.filter(slug=MAIN_TEACHER_SLUG).first()


def default_teacher_type() -> TeacherType | None:
    return (
        TeacherType.objects.filter(is_active=True, is_default=True).first()
        or TeacherType.objects.filter(is_active=True, slug=CO_TEACHER_SLUG).first()
    )


def resolve_assignment_type(assignment: CohortTeacher) -> TeacherType | None:
    """Resolve/backfill the canonical type for an old-node compatibility row."""
    if assignment.teacher_type_id:
        return assignment.teacher_type
    slug = type_slug_for_legacy_role(assignment.role) or CO_TEACHER_SLUG
    teacher_type = TeacherType.objects.filter(slug=slug).first()
    if teacher_type is not None:
        CohortTeacher.objects.filter(pk=assignment.pk, teacher_type__isnull=True).update(
            teacher_type=teacher_type
        )
        assignment.teacher_type = teacher_type
    return teacher_type


def refresh_primary_teacher(cohort: Cohort) -> int | None:
    """Project the canonical Main Teacher assignments onto the legacy FK.

    When several main teachers exist, an already-selected compatible teacher stays
    selected; otherwise the oldest assignment is chosen deterministically.
    """
    main_assignments = CohortTeacher.objects.filter(
        cohort_id=cohort.pk,
        teacher_type__slug=MAIN_TEACHER_SLUG,
    ).order_by("id")
    current_id: int | None = cohort.primary_teacher_id
    if current_id and main_assignments.filter(teacher_id=current_id).exists():
        projected_id: int | None = current_id
    else:
        projected_id = main_assignments.values_list("teacher_id", flat=True).first()
    if projected_id != current_id:
        Cohort.objects.filter(pk=cohort.pk).update(primary_teacher_id=projected_id)
        cohort.primary_teacher_id = projected_id
        cohort._state.fields_cache.pop("primary_teacher", None)
    return projected_id


@transaction.atomic
def sync_legacy_primary_assignment(cohort: Cohort, *, previous_primary_teacher_id: int | None) -> None:
    """Translate a legacy ``primary_teacher`` edit into canonical assignments."""
    teacher_type = main_teacher_type()
    if teacher_type is None:  # Only possible while migrations are in progress.
        return
    current_id = cohort.primary_teacher_id
    if previous_primary_teacher_id and previous_primary_teacher_id != current_id:
        CohortTeacher.objects.filter(
            cohort_id=cohort.pk,
            teacher_id=previous_primary_teacher_id,
            teacher_type=teacher_type,
        ).delete()
    if current_id:
        try:
            with transaction.atomic():
                CohortTeacher.objects.get_or_create(
                    cohort_id=cohort.pk,
                    teacher_id=current_id,
                    teacher_type=teacher_type,
                )
        except IntegrityError:
            # A concurrent legacy save may have created the same canonical triple.
            if not CohortTeacher.objects.filter(
                cohort_id=cohort.pk,
                teacher_id=current_id,
                teacher_type=teacher_type,
            ).exists():
                raise
    refresh_primary_teacher(cohort)
