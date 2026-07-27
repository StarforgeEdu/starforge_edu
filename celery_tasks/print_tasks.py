"""Celery tasks for the print-job lifecycle (D4-LD-6).

These tasks DO NOT talk to CUPS — the real dispatch lives in a separate branch
agent (different repo / deploy target, ADR-004, TASKS §28) that authenticates
with a hashed token and PULLS queued ``PrintJob`` rows via the agent endpoints.

``enqueue_print_job`` is the post-commit hook ``apps.printing.services.enqueue_print``
schedules: it confirms the job is queued and writes the ``print.job_created``
audit row. It is idempotent — re-delivery for an already-created job re-runs
harmlessly (the audit write is guarded on the job still being queued and an
existing creation row).
"""

from __future__ import annotations

import logging

from config.celery import app

logger = logging.getLogger("starforge.printing")


@app.task(bind=True, max_retries=3, default_retry_delay=30, acks_late=True)
def enqueue_print_job(self, print_job_id: int) -> dict[str, object]:
    """Mark the job ready for the branch agent + write the creation audit row.

    Idempotent: a redelivery does not create a duplicate audit row (guarded on a
    prior ``print.job_created`` row for this job).
    """
    from apps.audit.services import audit_log
    from apps.printing.models import PrintJob

    try:
        job = PrintJob.objects.get(pk=print_job_id)
    except PrintJob.DoesNotExist:
        logger.warning("enqueue_print_job: job %s gone", print_job_id)
        return {"print_job_id": print_job_id, "status": "missing"}

    if _already_audited(job_id=job.pk):
        return {"print_job_id": print_job_id, "status": "already_enqueued"}

    audit_log(
        actor=None,
        action="print.job_created",
        resource_type="printing.PrintJob",
        resource_id=job.pk,
        after={
            "source": job.source,
            "source_id": job.source_id,
            "branch_id": job.branch_id,
            "pages": job.pages,
            "copies": job.copies,
            "status": job.status,
        },
    )
    return {"print_job_id": print_job_id, "status": "enqueued"}


def _already_audited(*, job_id: int) -> bool:
    """True when a ``print.job_created`` audit row already exists for this job.

    Keeps ``enqueue_print_job`` idempotent under Celery's at-least-once delivery.
    On the public schema (no tenant audit table) this returns False — the
    ``audit_log`` call itself no-ops there.
    """
    from django_tenants.utils import get_public_schema_name

    from core.utils import current_schema

    if current_schema() == get_public_schema_name():
        return False
    from apps.audit.models import AuditLog

    return AuditLog.objects.filter(
        action="print.job_created", resource_type="printing.PrintJob", resource_id=str(job_id)
    ).exists()
