"""Day-1 consumer of cohort signals: a log line. Day 3 Lane D adds AuditLog."""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.cohorts.models import Cohort, CohortTeacher
from apps.cohorts.signals import cohort_member_moved

logger = logging.getLogger("starforge.cohorts")


@receiver(pre_save, sender=Cohort, dispatch_uid="cohorts.capture_previous_primary_teacher")
def capture_previous_primary_teacher(sender, instance: Cohort, **kwargs) -> None:
    if not instance.pk:
        instance._previous_primary_teacher_id = None  # type: ignore[attr-defined]
        return
    instance._previous_primary_teacher_id = (  # type: ignore[attr-defined]
        Cohort.objects.filter(pk=instance.pk).values_list("primary_teacher_id", flat=True).first()
    )


@receiver(post_save, sender=Cohort, dispatch_uid="cohorts.sync_legacy_primary_assignment")
def on_cohort_saved(sender, instance: Cohort, **kwargs) -> None:
    from apps.cohorts.teacher_assignments import sync_legacy_primary_assignment

    sync_legacy_primary_assignment(
        instance,
        previous_primary_teacher_id=getattr(instance, "_previous_primary_teacher_id", None),
    )


@receiver(post_save, sender=CohortTeacher, dispatch_uid="cohorts.project_primary_on_assignment_save")
def on_teacher_assignment_saved(sender, instance: CohortTeacher, **kwargs) -> None:
    from apps.cohorts.teacher_assignments import refresh_primary_teacher

    refresh_primary_teacher(instance.cohort)


@receiver(post_delete, sender=CohortTeacher, dispatch_uid="cohorts.project_primary_on_assignment_delete")
def on_teacher_assignment_deleted(sender, instance: CohortTeacher, **kwargs) -> None:
    from apps.cohorts.teacher_assignments import refresh_primary_teacher

    cohort = Cohort.objects.filter(pk=instance.cohort_id).first()
    if cohort is not None:
        refresh_primary_teacher(cohort)


@receiver(cohort_member_moved, dispatch_uid="cohorts.log_member_moved")
def on_cohort_member_moved(sender, *, student_id, to_cohort_id, schema_name="", **kwargs) -> None:
    logger.info(
        "cohort_member_moved student=%s to_cohort=%s schema=%s",
        student_id,
        to_cohort_id,
        schema_name,
    )
