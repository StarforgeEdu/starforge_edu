"""Finance signal receivers (D3-A-3): auto-issue an invoice on enrollment.

The auto-issue trigger is `apps.cohorts.signals.cohort_member_moved`. Both
`enroll_student_in_cohort` AND `move_student` emit it (via
`transaction.on_commit`), so this receiver fires on the initial enrollment as
well as subsequent cohort moves.

The receiver body is deliberately thin: it enqueues nothing heavy synchronously;
`auto_issue_on_enrollment` is idempotent (dedupe on (student, fee_schedule,
period)) so a re-fired signal creates no duplicate invoice.
"""

from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.cohorts.signals import cohort_member_moved

logger = logging.getLogger("starforge.finance")


@receiver(cohort_member_moved, dispatch_uid="finance.auto_issue_on_enrollment")
def on_cohort_member_moved(sender, *, student_id, to_cohort_id, schema_name="", **kwargs) -> None:
    """Issue (idempotently) the matching fee-schedule invoice when a student lands
    in a cohort. Runs in the active tenant schema (the signal is emitted inside
    `transaction.on_commit`, so the membership row is committed)."""
    from apps.finance.services import auto_issue_on_enrollment

    try:
        auto_issue_on_enrollment(student_id=student_id, cohort_id=to_cohort_id)
    except Exception:  # never let a notification/billing hiccup break enrollment
        logger.exception(
            "auto_issue_on_enrollment failed student=%s cohort=%s schema=%s",
            student_id,
            to_cohort_id,
            schema_name,
        )
