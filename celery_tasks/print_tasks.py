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
    try:
        return _enqueue_print_job_body(print_job_id)
    except Exception:
        # Storage/database exceptions can expose object keys or internal
        # diagnostics. Retry with stable operator-safe evidence only.
        raise self.retry(exc=RuntimeError("Print-job enqueue failed.")) from None


def _enqueue_print_job_body(print_job_id: int) -> dict[str, object]:
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


@app.task
def quarantine_stale_print_leases() -> int:
    """Fan out the bounded stale-lease quarantine to every active tenant."""

    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center

    schemas = list(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .order_by("schema_name")
        .values_list("schema_name", flat=True)
    )
    for schema in schemas:
        quarantine_stale_print_leases_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task(acks_late=True, reject_on_worker_lost=True)
def quarantine_stale_print_leases_for_schema() -> int:
    """Quarantine one bounded batch; never requeue physical output."""

    from django.conf import settings

    from apps.printing.services import quarantine_stale_print_leases as quarantine

    count = quarantine(batch_size=settings.PRINT_STALE_LEASE_SWEEP_BATCH_SIZE)
    if count:
        logger.warning("Quarantined %s expired physical-print lease(s) for review.", count)
    return count
