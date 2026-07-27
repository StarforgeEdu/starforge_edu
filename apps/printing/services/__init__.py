"""Printing write-side services (D4-LD-2..6).

All writes go through here: typed keyword-only signatures, ``@transaction.atomic``,
``StarforgeError`` subclasses, signals emitted via ``transaction.on_commit``.

Public contract (published to WORKLOG — transcripts/receipts/reports consume):

    enqueue_print(*, source, source_id, payload_s3_key, branch_id, requested_by,
                  pages, copies=1, color=False, duplex=False, cohort_id=None)
        -> PrintJob

    - Idempotent on an OPEN (not done/failed) job for (source, source_id,
      payload_s3_key): a duplicate call returns the existing job, no new row.
    - Enforces the per-cohort/term page quota (CenterSettings, 0 = unlimited).
    - Emits ``print_job_created`` + enqueues ``enqueue_print_job`` on commit.

The branch agent (separate repo, TASKS §28) pulls jobs via ``claim_job`` and
reports back via ``update_job_status``. No CUPS code lives here.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.printing.models import BranchAgent, Printer, PrintJob
from apps.printing.signals import print_job_created, print_job_failed
from core.exceptions import ConflictException, UnprocessableEntity, ValidationException
from core.utils import current_schema, stable_hash

# Open statuses: a job is still "in the queue" until it reaches done/failed.
OPEN_STATUSES = (
    PrintJob.Status.QUEUED,
    PrintJob.Status.PICKED,
    PrintJob.Status.PRINTING,
)

# Allowed agent status transitions (D4-LD-3). Anything else -> 409.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    PrintJob.Status.PICKED: {PrintJob.Status.PRINTING, PrintJob.Status.FAILED},
    PrintJob.Status.PRINTING: {PrintJob.Status.DONE, PrintJob.Status.FAILED},
}

# Max delivery attempts before a job is finally failed (D4-LD-4).
MAX_ATTEMPTS = 3
# Base backoff unit; next_attempt_at = now + 2**attempts * RETRY_BACKOFF_SECONDS.
RETRY_BACKOFF_SECONDS = 60
# Raw agent token length in bytes (hex-encoded -> 2x chars).
AGENT_TOKEN_BYTES = 32


# --------------------------------------------------------------------------- #
# Agent registration (D4-LD-2)
# --------------------------------------------------------------------------- #
@transaction.atomic
def register_agent(*, branch_id: int, name: str, created_by: Any = None) -> tuple[BranchAgent, str]:
    """Create a BranchAgent and return (agent, raw_token).

    Only the sha256 hash is persisted — the raw token is returned once and never
    stored. Callers surface it to the operator a single time.
    """
    raw_token = secrets.token_hex(AGENT_TOKEN_BYTES)
    agent = BranchAgent.objects.create(
        branch_id=branch_id,
        name=name,
        token_hash=stable_hash(raw_token),
        created_by=created_by if getattr(created_by, "pk", None) else None,
    )
    return agent, raw_token


def revoke_agent(*, agent_id: int) -> BranchAgent:
    agent = BranchAgent.objects.get(pk=agent_id)
    if agent.revoked_at is None:
        agent.revoked_at = timezone.now()
        agent.save(update_fields=["revoked_at"])
    return agent


# --------------------------------------------------------------------------- #
# Quotas (D4-LD-5)
# --------------------------------------------------------------------------- #
def _current_term_window() -> tuple[Any, Any] | None:
    """The (start_date, end_date) of the current term, or None if none is set.

    Quota is per-cohort *term*; the current term bounds the usage window. With no
    current term defined, there is no term window to meter against -> unlimited.
    """
    from apps.schedule.models import Term

    term = Term.objects.filter(is_current=True).order_by("-start_date").first()
    if term is None:
        return None
    return term.start_date, term.end_date


def _cohort_term_pages_used(*, cohort_id: int, window: tuple[Any, Any]) -> int:
    """Sum of pages*copies already enqueued for this cohort in the term window.

    Counts every non-failed job (a failed job did not consume the quota).
    """
    from django.db.models import F, Sum

    start, end = window
    used = (
        PrintJob.objects.filter(
            cohort_id=cohort_id,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
        .exclude(status=PrintJob.Status.FAILED)
        .aggregate(total=Sum(F("pages") * F("copies")))
        .get("total")
    )
    return int(used or 0)


def _assert_within_quota(*, cohort_id: int | None, pages: int, copies: int) -> None:
    """Raise ``print_quota_exceeded`` when this job would exceed the term quota."""
    from apps.org.selectors import get_center_settings

    quota = getattr(get_center_settings(), "print_quota_pages_per_cohort_term", 0)
    if not quota:  # 0 / None = unlimited
        return
    if cohort_id is None:
        return  # no cohort attribution -> not metered against a cohort quota
    window = _current_term_window()
    if window is None:
        return
    used = _cohort_term_pages_used(cohort_id=cohort_id, window=window)
    requested = pages * copies
    if used + requested > quota:
        raise UnprocessableEntity(
            _("Print quota for this cohort's term has been exceeded."),
            code="print_quota_exceeded",
            fields={"quota": quota, "used": used, "requested": requested},
        )


# --------------------------------------------------------------------------- #
# Enqueue (D4-LD-6) — the public hook
# --------------------------------------------------------------------------- #
@transaction.atomic
def enqueue_print(
    *,
    source: str,
    source_id: int,
    payload_s3_key: str,
    branch_id: int,
    requested_by: Any,
    pages: int,
    copies: int = 1,
    color: bool = False,
    duplex: bool = False,
    cohort_id: int | None = None,
) -> PrintJob:
    """Create (idempotently) a queued PrintJob and schedule the agent hand-off."""
    if source not in PrintJob.Source.values:
        raise ValidationException(_("Unknown print source."), code="invalid_source")
    if pages < 1:
        raise ValidationException(_("A print job must have at least one page."), code="invalid_pages")
    if copies < 1:
        raise ValidationException(_("A print job must have at least one copy."), code="invalid_copies")

    # Idempotency: an OPEN job for the same (branch, source, source_id, payload
    # key) is a no-op — return it (a duplicate transcript/receipt/report hand-off).
    # branch_id MUST be in the filter: two branches can legitimately submit the
    # same payload key, and without it branch B's job would be silently routed to
    # branch A's agent (claim_job filters by branch).
    existing = (
        PrintJob.objects.filter(
            branch_id=branch_id,
            source=source,
            source_id=source_id,
            payload_s3_key=payload_s3_key,
            status__in=OPEN_STATUSES,
        )
        .order_by("created_at")
        .first()
    )
    if existing is not None:
        return existing

    _assert_within_quota(cohort_id=cohort_id, pages=pages, copies=copies)

    try:
        # The savepoint keeps the outer transaction usable if a concurrent
        # request wins the partial unique constraint between our SELECT and
        # INSERT. PostgreSQL waits for that transaction before raising, so the
        # winning open job is visible to the recovery query below.
        with transaction.atomic():
            job = PrintJob.objects.create(
                branch_id=branch_id,
                status=PrintJob.Status.QUEUED,
                source=source,
                source_id=source_id,
                payload_s3_key=payload_s3_key,
                pages=pages,
                copies=copies,
                color=color,
                duplex=duplex,
                cohort_id=cohort_id,
                requested_by=requested_by if getattr(requested_by, "pk", None) else None,
                next_attempt_at=timezone.now(),
            )
    except IntegrityError:
        existing = (
            PrintJob.objects.filter(
                branch_id=branch_id,
                source=source,
                source_id=source_id,
                payload_s3_key=payload_s3_key,
                status__in=OPEN_STATUSES,
            )
            .order_by("created_at")
            .first()
        )
        if existing is None:
            raise
        return existing

    schema_name = current_schema()
    job_id = job.pk

    def _post_commit() -> None:
        print_job_created.send(
            sender=PrintJob,
            job_id=job_id,
            source=source,
            source_id=source_id,
            branch_id=branch_id,
            schema_name=schema_name,
        )
        from celery_tasks.print_tasks import enqueue_print_job

        enqueue_print_job.delay(job_id, _schema_name=schema_name)

    transaction.on_commit(_post_commit)
    return job


def _assign_least_loaded_printer(job: PrintJob) -> None:
    """Round-robin balance across the branch's ACTIVE printers (F16-1): assign the job
    to the printer currently carrying the fewest in-flight (picked/printing) jobs, ties
    broken by id. Leaves the printer unset when the branch registered none — the agent
    then falls back to its own default device. Keeps no single printer overloaded while
    the rest sit idle. SELECT-only (sets job.printer in memory); the caller persists it
    inside claim_job's transaction, so no decorator of its own."""
    from django.db.models import Count

    printers = list(Printer.objects.filter(branch_id=job.branch_id, is_active=True).order_by("id"))
    if not printers:
        return
    load = {p.id: 0 for p in printers}
    for row in (
        PrintJob.objects.filter(
            branch_id=job.branch_id,
            printer_id__in=load,
            status__in=(PrintJob.Status.PICKED, PrintJob.Status.PRINTING),
        )
        .values("printer_id")
        .annotate(n=Count("id"))
    ):
        load[row["printer_id"]] = row["n"]
    job.printer = min(printers, key=lambda p: (load[p.id], p.id))


# --------------------------------------------------------------------------- #
# Agent claim (D4-LD-3) — atomic, branch-scoped
# --------------------------------------------------------------------------- #
@transaction.atomic
def claim_job(*, agent: BranchAgent) -> PrintJob | None:
    """Atomically claim the oldest claimable queued job for the agent's branch.

    ``select_for_update(skip_locked=True)`` guarantees two concurrent claims
    never return the same row. Only jobs whose ``next_attempt_at`` has arrived
    (retry backoff) are eligible. Returns None when the queue is empty. On claim the
    job is round-robin balanced onto the least-loaded active printer (F16-1).
    """
    now = timezone.now()
    job = (
        PrintJob.objects.select_for_update(skip_locked=True)
        .filter(
            branch_id=agent.branch_id,
            status=PrintJob.Status.QUEUED,
            next_attempt_at__lte=now,
        )
        .order_by("created_at")
        .first()
    )
    if job is None:
        return None

    job.status = PrintJob.Status.PICKED
    job.agent = agent
    job.claimed_at = now
    if job.printer_id is None:
        _assign_least_loaded_printer(job)
    job.save(update_fields=["status", "agent", "claimed_at", "printer"])

    BranchAgent.objects.filter(pk=agent.pk).update(last_seen_at=now)
    return job


# --------------------------------------------------------------------------- #
# Agent status report (D4-LD-3/4) — transition matrix + retry policy
# --------------------------------------------------------------------------- #
@transaction.atomic
def update_job_status(
    *,
    agent: BranchAgent,
    job_id: int,
    status: str,
    error: str = "",
    pages_printed: int | None = None,
) -> PrintJob:
    """Apply an agent-reported status transition (picked->printing->done|failed).

    Cross-branch updates 404 (the agent can only touch its own branch's jobs).
    Illegal transitions raise 409 ``invalid_transition``. A ``failed`` report
    with attempts left re-queues with backoff; the final failure dispatches
    ``print.failed`` + audits.
    """
    now = timezone.now()
    BranchAgent.objects.filter(pk=agent.pk).update(last_seen_at=now)

    try:
        job = PrintJob.objects.select_for_update().get(pk=job_id, branch_id=agent.branch_id)
    except PrintJob.DoesNotExist as exc:
        from core.exceptions import NotFoundException

        raise NotFoundException(_("Print job not found."), code="not_found") from exc

    allowed = _ALLOWED_TRANSITIONS.get(job.status, set())
    if status not in allowed:
        raise ConflictException(
            _("Illegal print job status transition."),
            code="invalid_transition",
            fields={"from": job.status, "to": status},
        )

    if pages_printed is not None:
        job.pages_printed = pages_printed

    if status == PrintJob.Status.PRINTING:
        job.status = PrintJob.Status.PRINTING
        job.save(update_fields=["status", "pages_printed"])
        return job

    if status == PrintJob.Status.DONE:
        job.status = PrintJob.Status.DONE
        job.finished_at = now
        job.save(update_fields=["status", "finished_at", "pages_printed"])
        _audit_job(job, action="print.job_done")
        return job

    # status == FAILED — apply retry policy (D4-LD-4).
    job.attempts += 1
    job.last_error = (error or "")[:2000]
    job.agent = None

    if job.attempts < MAX_ATTEMPTS:
        backoff = (2**job.attempts) * RETRY_BACKOFF_SECONDS
        job.status = PrintJob.Status.QUEUED
        job.next_attempt_at = now + timedelta(seconds=backoff)
        # Clear the printer so the retry rebalances onto the least-loaded ACTIVE printer
        # (F16-1) — print failures are often printer-specific (offline/jam/inactive), so
        # a retry must not be pinned to the device that just failed it.
        job.printer = None
        job.save(
            update_fields=[
                "status",
                "attempts",
                "last_error",
                "agent",
                "printer",
                "next_attempt_at",
                "pages_printed",
            ]
        )
        return job

    # Final failure: no more retries.
    job.status = PrintJob.Status.FAILED
    job.finished_at = now
    job.next_attempt_at = None
    job.save(
        update_fields=[
            "status",
            "attempts",
            "last_error",
            "agent",
            "finished_at",
            "next_attempt_at",
            "pages_printed",
        ]
    )
    _audit_job(job, action="print.job_failed")

    schema_name = current_schema()
    requested_by_id = job.requested_by_id
    job_pk = job.pk
    source = job.source
    source_id = job.source_id

    def _post_commit() -> None:
        print_job_failed.send(
            sender=PrintJob,
            job_id=job_pk,
            requested_by_id=requested_by_id,
            source=source,
            source_id=source_id,
            schema_name=schema_name,
        )
        if requested_by_id is not None:
            from apps.notifications.models import EventType
            from apps.notifications.services import dispatch

            dispatch(
                event_type=EventType.PRINT_JOB_FAILED,
                recipient_id=requested_by_id,
                context={"job_id": job_pk, "source": source, "source_id": source_id},
                dedupe_key=f"print.failed:{schema_name}:{job_pk}",
            )

    transaction.on_commit(_post_commit)
    return job


def _audit_job(job: PrintJob, *, action: str) -> None:
    """Append a print audit row (TD-9). Lazy import — cross-app, never at module load."""
    from apps.audit.services import audit_log

    audit_log(
        actor=None,
        action=action,
        resource_type="printing.PrintJob",
        resource_id=job.pk,
        after={
            "source": job.source,
            "source_id": job.source_id,
            "pages": job.pages,
            "copies": job.copies,
            "status": job.status,
            "attempts": job.attempts,
        },
    )
