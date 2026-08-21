"""Report Celery tasks (D4-LB-4/6/7).

* ``build_report(run_id)`` — one-shot render → S3 → signed URL via
  ``notifications.dispatch``. Idempotent (a run not in ``queued`` is skipped),
  retries ≤3 with exponential backoff. Runs under the scheduling tenant's schema
  (enqueued with ``_schema_name``).
* ``run_due_report_schedules`` — hourly per-tenant scan; fires due
  ``ReportSchedule`` rows (``last_run_at`` guard). Public dispatcher iterates
  active Centers; the per-tenant body runs inside each tenant schema.
No weasyprint/openpyxl/S3 call happens in a request handler (DoD #9).
"""

from __future__ import annotations

import logging

from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from config.celery import app

logger = logging.getLogger("starforge.reports")


# --------------------------------------------------------------------------- #
# One-shot generation (D4-LB-4)
# --------------------------------------------------------------------------- #
@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def build_report(self, run_id: int) -> str | None:
    from apps.reports.services import build_report_run, mark_run_failed, reset_run_for_retry

    try:
        return build_report_run(run_id)
    except Exception as exc:
        from core.exceptions import ConflictException

        if isinstance(exc, ConflictException) and exc.code == "report_in_progress":
            # A duplicate delivery must not reset the original worker's RUNNING
            # row back to QUEUED. Retry and observe its terminal result.
            raise self.retry(exc=exc, countdown=5) from exc
        # Only persist FAILED once the retry budget is exhausted. On an earlier
        # attempt, reset the run to QUEUED so the retry actually re-executes —
        # build_report_run flips it to RUNNING before working and early-returns on
        # any non-QUEUED status, so without the reset every retry was a dead no-op.
        if self.request.retries >= self.max_retries:
            mark_run_failed(run_id, exc)
            raise
        reset_run_for_retry(run_id)
        raise self.retry(exc=exc) from exc


# --------------------------------------------------------------------------- #
# Hourly schedule scan (D4-LB-6)
# --------------------------------------------------------------------------- #
def _active_centers():
    from apps.tenancy.models import Center

    with schema_context(get_public_schema_name()):
        return list(Center.objects.filter(is_active=True).exclude(schema_name=get_public_schema_name()))


@app.task
def run_due_report_schedules() -> int:
    """Public dispatcher: scan every active tenant for due schedules. Returns the
    number of tenants scanned (per-tenant fan-out enqueues the actual runs)."""
    centers = _active_centers()
    for center in centers:
        run_due_report_schedules_for_schema.delay(_schema_name=center.schema_name)
    return len(centers)


@app.task
def run_due_report_schedules_for_schema() -> int:
    """Per-tenant body (runs inside the tenant schema via ``_schema_name``).
    Fires due schedules; returns the count fired."""
    from apps.reports.services import run_due_schedules

    return run_due_schedules(now=timezone.now())
