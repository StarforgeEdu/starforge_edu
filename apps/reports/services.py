"""Reports write-side services (D4-LB-4/5/6).

Creating a ``ReportRun`` enqueues ``build_report`` on commit; the task renders the
generator output, uploads to S3, and delivers a signed URL through
``notifications.dispatch`` (never the email client directly — DoD/§docs). The
hourly schedule scan (``run_due_report_schedules``) fires due ``ReportSchedule``
rows, guarded by ``last_run_at`` so re-running within the cadence window is a
no-op.
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.reports.generators import get_generator
from apps.reports.models import Report, ReportFormat, ReportRun, ReportSchedule
from core.exceptions import PermissionException, ValidationException
from core.permissions import Role
from core.utils import current_schema

logger = logging.getLogger("starforge.reports")

# The dispatch event name carried to the in-app/WS channel (Lane C consumes it).
REPORT_READY_EVENT = "report.ready"

# Roles that bypass per-report allowed_roles (see the whole library).
_FULL_ROLES = {Role.DIRECTOR}


def can_run_report(*, report: Report, roles: set[str], is_superuser: bool = False) -> bool:
    """True when a caller with these roles may run/see ``report``.

    Director / superuser always; otherwise the role must be in the report's
    ``allowed_roles`` list AND hold ``reports:write`` (checked by the view's
    permission class for the action; this is the per-report visibility gate).
    """
    if is_superuser or (roles & _FULL_ROLES):
        return True
    allowed = set(report.allowed_roles or [])
    return bool(roles & allowed)


@transaction.atomic
def create_report_run(
    *, report_key: str, fmt: str | None, params: dict[str, Any], requested_by, roles: set[str]
) -> ReportRun:
    """Validate the key/format/visibility, create a queued ReportRun, enqueue
    ``build_report`` after commit. Raises 403 when the caller's roles are not in
    the report's allowed_roles, 422 for an unknown key/format."""
    get_generator(report_key)  # 422 unknown_report_key
    try:
        report = Report.objects.get(key=report_key)
    except Report.DoesNotExist as exc:
        raise ValidationException(code="unknown_report_key") from exc

    is_superuser = bool(getattr(requested_by, "is_superuser", False))
    if not can_run_report(report=report, roles=roles, is_superuser=is_superuser):
        raise PermissionException(code="report_forbidden")

    chosen = fmt or report.default_format
    if chosen not in ReportFormat.values:
        raise ValidationException(code="invalid_format")

    run = ReportRun.objects.create(
        report=report,
        requested_by=requested_by,
        params=params or {},
        format=chosen,
        status=ReportRun.Status.QUEUED,
    )
    schema = current_schema()
    run_id = run.pk
    transaction.on_commit(lambda: _enqueue_build(run_id, schema))
    return run


def _enqueue_build(run_id: int, schema: str) -> None:
    from celery_tasks.report_tasks import build_report

    build_report.delay(run_id, _schema_name=schema)


# --------------------------------------------------------------------------- #
# build_report body (called by the Celery task — D4-LB-4)
# --------------------------------------------------------------------------- #
def build_report_run(run_id: int) -> str | None:
    """Idempotent task body: queued → running → done | failed.

    Renders the generator output for the run's scoping (the requester's roles),
    uploads to ``{schema}/reports/{run_id}.{ext}``, presigns a download URL, and
    dispatches a ``report.ready`` notification to the requester. A run not in
    ``queued`` is skipped (safe re-delivery). Returns the s3 key, or None when
    skipped.
    """
    from infrastructure.storage import s3_client

    run = ReportRun.objects.select_related("report", "requested_by").get(pk=run_id)
    if run.status in (ReportRun.Status.DONE, ReportRun.Status.FAILED):
        # Terminal — a re-delivery is a no-op (idempotent).
        return run.s3_key or None
    # QUEUED or RUNNING: RUNNING means a prior worker was hard-killed (OOM/SIGKILL)
    # mid-render before its `except` could reset the run — build_report is acks_late,
    # so the broker redelivers the task. Re-DRIVE it (render is idempotent: it
    # overwrites the same S3 key), rather than early-returning and stranding the run
    # in RUNNING forever with no file, no notification, no failure, no retry.

    run.status = ReportRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])

    generator = get_generator(run.report.key)
    roles = _requester_roles(run.requested_by)
    data = generator.collect(run.params or {}, user=run.requested_by, roles=roles)

    locale = _requester_locale(run.requested_by)
    payload = generator.render(data, run.format, locale=locale)

    ext = "xlsx" if run.format == ReportFormat.XLSX else "pdf"
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if run.format == ReportFormat.XLSX
        else "application/pdf"
    )
    key = f"{current_schema()}/reports/{run.pk}.{ext}"
    s3_client.upload_bytes(key, payload, content_type=content_type)

    run.s3_key = key
    run.file_bytes = len(payload)
    run.status = ReportRun.Status.DONE
    run.finished_at = timezone.now()
    run.save(update_fields=["s3_key", "file_bytes", "status", "finished_at"])

    _notify_ready(run)
    return key


def mark_run_failed(run_id: int, exc: Exception) -> None:
    ReportRun.objects.filter(pk=run_id).exclude(status=ReportRun.Status.DONE).update(
        status=ReportRun.Status.FAILED,
        error=str(exc)[:2000],
        finished_at=timezone.now(),
    )


def reset_run_for_retry(run_id: int) -> None:
    """Put a RUNNING/mid-flight run back to QUEUED so a Celery retry re-executes
    it. build_report_run early-returns on any non-QUEUED status and flips the run
    to RUNNING before doing work, so without this reset every retry would
    short-circuit and the retry budget was a dead no-op."""
    ReportRun.objects.filter(pk=run_id).exclude(status=ReportRun.Status.DONE).update(
        status=ReportRun.Status.QUEUED,
        started_at=None,
    )


def _requester_roles(user) -> set[str]:
    if user is None:
        return set()
    return {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}


def _requester_locale(user) -> str:
    return getattr(user, "preferred_language", "") or "uz"


def presign_run(run: ReportRun) -> str | None:
    """A fresh presigned download URL — only when the run is done."""
    if run.status == ReportRun.Status.DONE and run.s3_key:
        from infrastructure.storage import s3_client

        return s3_client.presign_download(run.s3_key, expires_in=600)
    return None


def _notify_ready(run: ReportRun) -> None:
    """Deliver the ready signed URL via notifications.dispatch (never email
    directly). Recipients are the requester PLUS any ``recipient_ids`` configured
    on the originating schedule (deduped); a fresh presign is embedded so the
    in-app/WS payload carries a working link."""
    # Preserve order (requester first), dedupe, drop falsy ids.
    recipients: list[int] = []
    for rid in [run.requested_by_id, *(run.recipient_ids or [])]:
        if rid and rid not in recipients:
            recipients.append(rid)
    if not recipients:
        return
    from apps.notifications.services import dispatch

    schema = current_schema()
    context = {
        "report": run.report.key,
        "report_title": run.report.title,
        "run_id": run.pk,
        "format": run.format,
        "download_url": presign_run(run) or "",
    }
    for rid in recipients:
        dispatch(
            event_type=REPORT_READY_EVENT,
            recipient_id=rid,
            context=context,
            # Per-recipient key so the in-app dedupe doesn't collapse deliveries.
            dedupe_key=f"report.ready:{schema}:{run.pk}:{rid}",
        )


# --------------------------------------------------------------------------- #
# Schedules (D4-LB-6)
# --------------------------------------------------------------------------- #
@transaction.atomic
def create_schedule(*, report_key: str, created_by, roles: set[str], **fields: Any) -> ReportSchedule:
    try:
        report = Report.objects.get(key=report_key)
    except Report.DoesNotExist as exc:
        raise ValidationException(code="unknown_report_key") from exc
    is_superuser = bool(getattr(created_by, "is_superuser", False))
    if not can_run_report(report=report, roles=roles, is_superuser=is_superuser):
        raise PermissionException(code="report_forbidden")
    return ReportSchedule.objects.create(report=report, created_by=created_by, **fields)


def schedule_is_due(schedule: ReportSchedule, *, now: datetime) -> bool:
    """True when ``schedule`` should fire at ``now``: cadence anchor matches the
    current weekday/day-of-month + hour, and it hasn't already run this window.

    The ``last_run_at`` guard rejects a second fire within the same cadence period
    (a re-run of the hourly scan creates no duplicate run)."""
    if not schedule.is_active:
        return False
    local = timezone.localtime(now)
    if local.hour != schedule.hour:
        return False
    if schedule.cadence == ReportSchedule.Cadence.WEEKLY:
        if local.weekday() != schedule.weekday:
            return False
        window = timedelta(days=7)
    elif schedule.cadence == ReportSchedule.Cadence.MONTHLY:
        # Clamp the anchor to the month's last day so day_of_month in {29,30,31}
        # still fires in shorter months (Feb/Apr/Jun/Sep/Nov) instead of being
        # silently skipped. e.g. "the 31st" fires on Feb 28/29.
        last_day = calendar.monthrange(local.year, local.month)[1]
        target_day = min(schedule.day_of_month or 1, last_day)
        if local.day != target_day:
            return False
        window = timedelta(days=28)
    else:  # pragma: no cover - constrained by the model
        return False
    # last_run_at guard: reject a second fire within the same cadence window
    # (a re-run of the hourly scan must create no duplicate run).
    return not (schedule.last_run_at is not None and now - schedule.last_run_at < window)


@transaction.atomic
def fire_schedule(schedule: ReportSchedule, *, now: datetime) -> ReportRun:
    """Create a queued ReportRun for a due schedule and stamp ``last_run_at``.

    Locks the schedule row so two concurrent scans can't both fire it; re-checks
    due-ness under the lock (the last_run_at guard) before creating the run.
    """
    locked = ReportSchedule.objects.select_for_update().get(pk=schedule.pk)
    if not schedule_is_due(locked, now=now):
        # Lost the race — another scan already fired it.
        raise ValidationException(code="schedule_not_due")
    if locked.created_by_id is None:
        # The creator was deleted (SET_NULL). Without a requester there is no role
        # scope to generate against, so the run would be empty AND undelivered.
        # Refuse to create it here (deactivation is done by run_due_schedules,
        # OUTSIDE this atomic block — deactivating here would be rolled back by the
        # raise below).
        raise ValidationException(code="schedule_no_creator")
    run = ReportRun.objects.create(
        report=locked.report,
        requested_by=locked.created_by,
        params=locked.params or {},
        recipient_ids=list(locked.recipient_ids or []),
        format=locked.format,
        status=ReportRun.Status.QUEUED,
    )
    locked.last_run_at = now
    locked.save(update_fields=["last_run_at"])
    schema = current_schema()
    run_id = run.pk
    transaction.on_commit(lambda: _enqueue_build(run_id, schema))
    return run


def run_due_schedules(*, now: datetime | None = None) -> int:
    """Scan the current tenant's active schedules and fire the due ones. Returns
    the count fired. Idempotent within a cadence window (last_run_at guard)."""
    now = now or timezone.now()
    fired = 0
    candidates = list(ReportSchedule.objects.select_related("report", "created_by").filter(is_active=True))
    for schedule in candidates:
        if not schedule_is_due(schedule, now=now):
            continue
        if schedule.created_by_id is None:
            # Creator deleted → no scope/recipient. Deactivate (committed here,
            # outside any atomic) so it stops firing empty, undelivered runs.
            ReportSchedule.objects.filter(pk=schedule.pk).update(is_active=False)
            logger.warning("report schedule %s deactivated: creator deleted", schedule.pk)
            continue
        try:
            fire_schedule(schedule, now=now)
            fired += 1
        except ValidationException:
            continue  # lost the race / no longer due
    return fired
