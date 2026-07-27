"""Day-1 consumer of cohort signals: a log line. Day 3 Lane D adds AuditLog."""

from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.cohorts.signals import cohort_member_moved

logger = logging.getLogger("starforge.cohorts")


@receiver(cohort_member_moved, dispatch_uid="cohorts.log_member_moved")
def on_cohort_member_moved(sender, *, student_id, to_cohort_id, schema_name="", **kwargs) -> None:
    logger.info(
        "cohort_member_moved student=%s to_cohort=%s schema=%s",
        student_id,
        to_cohort_id,
        schema_name,
    )
