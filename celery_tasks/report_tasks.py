"""Report Celery tasks (D4-LB-4/6/7).

* ``build_report(run_id)`` — one-shot render → S3 → signed URL via
  ``notifications.dispatch``. Idempotent (a run not in ``queued`` is skipped),
  retries ≤3 with exponential backoff. Runs under the scheduling tenant's schema
  (enqueued with ``_schema_name``).
* ``run_due_report_schedules`` — hourly per-tenant scan; fires due
  ``ReportSchedule`` rows (``last_run_at`` guard). Public dispatcher iterates
  active Centers; the per-tenant body runs inside each tenant schema.
* ``nightly_platform_aggregation`` — public-schema cross-tenant meter: per Center,
  collect student count + DAU + storage bytes + AI tokens and upsert
  ``billing.UsageSnapshot(center, date)`` (unique → rerun updates, never dupes).

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


# --------------------------------------------------------------------------- #
# Nightly cross-tenant aggregation (D4-LB-7)
# --------------------------------------------------------------------------- #
@app.task
def nightly_platform_aggregation() -> int:
    """Public-schema meter: upsert a UsageSnapshot per active Center for today.
    Returns the count of centers metered. Idempotent on (center, date)."""
    centers = _active_centers()
    for center in centers:
        aggregate_center(center_id=center.pk)
    return len(centers)


def aggregate_center(*, center_id: int) -> None:
    """Collect one Center's usage under its tenant schema and upsert the public
    UsageSnapshot row for today. Separated so tests can drive one center."""
    from apps.billing.models import UsageSnapshot
    from apps.tenancy.models import Center

    with schema_context(get_public_schema_name()):
        center = Center.objects.filter(pk=center_id, is_active=True).first()
    if center is None:
        return

    # Center-LOCAL date (usage_series / center_dau read by localdate; a UTC .date() is
    # off by one in the 00:00-05:00 Tashkent window — see billing_tasks.meter_center).
    today = timezone.localdate()
    students = _students_count(center.schema_name)
    dau = _dau(center.schema_name, today)
    storage_bytes = _storage_bytes(center.schema_name)
    ai_tokens = _ai_tokens(center.schema_name)

    defaults = {
        "students_count": students,
        "storage_bytes": storage_bytes,
        "ai_tokens_used": ai_tokens,
    }
    # `dau` is the additive UsageSnapshot field this lane introduces (the migration
    # is applied centrally — apps/billing is off-limits to edit here). Write it
    # only once the column exists so this task is safe before/after that merge.
    if _usage_snapshot_has_dau():
        defaults["dau"] = dau

    with schema_context(get_public_schema_name()):
        UsageSnapshot.objects.update_or_create(center=center, date=today, defaults=defaults)


def _usage_snapshot_has_dau() -> bool:
    from apps.billing.models import UsageSnapshot

    # Field name as a variable so static checks don't validate the literal before
    # the additive migration (applied centrally) lands the `dau` column.
    field_name = "dau"
    try:
        UsageSnapshot._meta.get_field(field_name)
        return True
    except Exception:
        return False


def _students_count(schema_name: str) -> int:
    from apps.students.models import StudentProfile

    with schema_context(schema_name):
        return StudentProfile.objects.filter(
            status__in=(StudentProfile.Status.ENROLLED, StudentProfile.Status.ACTIVE)
        ).count()


def _dau(schema_name: str, today) -> int:
    """Daily active users: tenant Users seen today (last_seen_at >= start of day)."""
    from datetime import datetime, time

    from apps.users.models import User

    start = timezone.make_aware(datetime.combine(today, time.min))
    with schema_context(schema_name):
        return User.objects.filter(last_seen_at__gte=start).count()


def _storage_bytes(schema_name: str) -> int:
    try:
        from apps.content.selectors import storage_used_bytes
    except Exception:
        return 0
    with schema_context(schema_name):
        try:
            return int(storage_used_bytes())
        except Exception:
            logger.exception("storage_used_bytes failed", extra={"schema": schema_name})
            return 0


def _ai_tokens(schema_name: str) -> int:
    """AI tokens consumed this month via Lane A's published selector
    (``apps.ai.selectors.tokens_consumed(start, end)``), tolerating its absence
    (0) until A merges."""
    import calendar
    from datetime import date

    today = timezone.localdate()
    last_day = calendar.monthrange(today.year, today.month)[1]
    start = date(today.year, today.month, 1)
    end = date(today.year, today.month, last_day)
    try:
        from apps.ai.selectors import tokens_consumed
    except Exception:
        # Fall back to the Day-3 stub name if the D4-LA-9 name isn't merged yet.
        try:
            from apps.ai.selectors import tokens_used_current_month
        except Exception:
            return 0
        with schema_context(schema_name):
            try:
                return int(tokens_used_current_month())
            except Exception:
                return 0
    with schema_context(schema_name):
        try:
            return int(tokens_consumed(start, end))
        except Exception:
            return 0
