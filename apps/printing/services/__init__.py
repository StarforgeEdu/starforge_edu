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
import unicodedata
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.printing.models import BranchAgent, Printer, PrintJob, PrintJobReconciliation
from apps.printing.signals import print_job_created, print_job_failed
from core.exceptions import ConflictException, UnprocessableEntity, ValidationException
from core.utils import current_schema, stable_hash

# Open statuses: a job is still "in the queue" until it reaches done/failed.
OPEN_STATUSES = (
    PrintJob.Status.QUEUED,
    PrintJob.Status.PICKED,
    PrintJob.Status.PRINTING,
    PrintJob.Status.RECONCILIATION_REQUIRED,
)

ACTIVE_LEASE_STATUSES = (
    PrintJob.Status.PICKED,
    PrintJob.Status.PRINTING,
)

# Allowed agent status transitions (D4-LD-3). Anything else -> 409.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    PrintJob.Status.PICKED: {PrintJob.Status.PRINTING, PrintJob.Status.FAILED},
    # PRINTING is an idempotent exact-lease retry. An agent whose transition
    # response was lost can safely confirm the state without submitting the
    # document twice or inventing a new attempt.
    PrintJob.Status.PRINTING: {
        PrintJob.Status.PRINTING,
        PrintJob.Status.DONE,
        PrintJob.Status.FAILED,
    },
}

# Max delivery attempts before a job is finally failed (D4-LD-4).
MAX_ATTEMPTS = 3
# Base backoff unit; next_attempt_at = now + 2**attempts * RETRY_BACKOFF_SECONDS.
RETRY_BACKOFF_SECONDS = 60
# Raw agent token length in bytes (hex-encoded -> 2x chars).
AGENT_TOKEN_BYTES = 32

_RECONCILIATION_OUTCOMES = set(PrintJobReconciliation.Outcome.values)


def print_agent_lease_seconds() -> int:
    """Return the reviewed physical-delivery lease duration.

    Production validates this at startup. The service repeats the narrow type
    and range check so tests, management commands, or a dynamically overridden
    setting still fail closed before granting a capability.
    """

    value = getattr(settings, "PRINT_AGENT_LEASE_SECONDS", 600)
    if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 60 * 60:
        raise ImproperlyConfigured("PRINT_AGENT_LEASE_SECONDS must be between 60 and 3600.")
    return value


def _lease_deadline(now) -> Any:
    return now + timedelta(seconds=print_agent_lease_seconds())


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


@transaction.atomic
def revoke_agent(*, agent_id: int) -> BranchAgent:
    # Serialize revocation with claim/status operations, which take this same
    # agent lock before touching a job. Once this update commits, a stale
    # in-flight token cannot begin another mutation.
    agent = BranchAgent.objects.select_for_update().get(pk=agent_id)
    if agent.revoked_at is None:
        agent.revoked_at = timezone.now()
        agent.save(update_fields=["revoked_at"])
    return agent


def _lock_active_agent(agent: BranchAgent) -> BranchAgent:
    """Revalidate a previously authenticated agent at the mutation boundary."""

    live_agent = (
        BranchAgent.objects.select_for_update()
        .filter(
            pk=agent.pk,
            revoked_at__isnull=True,
        )
        .first()
    )
    if live_agent is None:
        from core.exceptions import AuthenticationException

        raise AuthenticationException(_("Invalid agent token."), code="agent_token_invalid")
    return live_agent


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

    Counts open and completed jobs. An explicitly unknown physical outcome is
    counted conservatively because paper may have been produced.
    """
    from django.db.models import F, Q, Sum

    start, end = window
    used = (
        PrintJob.objects.filter(
            Q(status__in=OPEN_STATUSES)
            | Q(status=PrintJob.Status.DONE)
            | Q(status=PrintJob.Status.FAILED, last_error="physical_output_unknown"),
            cohort_id=cohort_id,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
        .aggregate(total=Sum(F("pages") * F("copies")))
        .get("total")
    )
    return int(used or 0)


def _assert_within_quota(
    *,
    branch_id: int,
    cohort_id: int | None,
    source: str,
    source_id: int,
    payload_s3_key: str,
    pages: int,
    copies: int,
    color: bool,
    duplex: bool,
) -> PrintJob | None:
    """Raise ``print_quota_exceeded`` when this job would exceed the term quota."""
    from apps.org.selectors import get_center_settings

    quota = getattr(get_center_settings(), "print_quota_pages_per_cohort_term", 0)
    if not quota:  # 0 / None = unlimited
        return None
    if cohort_id is None:
        return None  # no cohort attribution -> not metered against a cohort quota
    window = _current_term_window()
    if window is None:
        return None
    # Serialize the read-total/write-job critical section for one cohort. The
    # PrintJob uniqueness constraint cannot protect distinct source rows, so
    # without this lock two concurrent requests can both observe the same
    # remaining allowance and together exceed it. Also reject an internal
    # producer that attaches another branch's cohort to this job.
    from apps.cohorts.models import Cohort

    cohort = Cohort.objects.select_for_update().only("pk").filter(pk=cohort_id, branch_id=branch_id).first()
    if cohort is None:
        raise ValidationException(
            _("The print job cohort is outside its branch."),
            code="invalid_cohort_scope",
            fields={"cohort": [_("Choose a cohort in the print job branch.")]},
        )
    # A concurrent exact retry may have committed while this transaction waited
    # for the cohort row. Re-read it under the serialization lock before charging
    # quota, otherwise a successful first request at the limit makes its retry
    # fail with print_quota_exceeded instead of returning the same open job.
    existing = _existing_open_job(
        branch_id=branch_id,
        source=source,
        source_id=source_id,
        payload_s3_key=payload_s3_key,
    )
    if existing is not None:
        return _assert_exact_print_retry(
            existing,
            pages=pages,
            copies=copies,
            color=color,
            duplex=duplex,
            cohort_id=cohort_id,
        )
    used = _cohort_term_pages_used(cohort_id=cohort_id, window=window)
    requested = pages * copies
    if used + requested > quota:
        raise UnprocessableEntity(
            _("Print quota for this cohort's term has been exceeded."),
            code="print_quota_exceeded",
            fields={"quota": quota, "used": used, "requested": requested},
        )
    return None


def _existing_open_job(
    *,
    branch_id: int,
    source: str,
    source_id: int,
    payload_s3_key: str,
) -> PrintJob | None:
    return (
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


def _assert_exact_print_retry(
    existing: PrintJob,
    *,
    pages: int,
    copies: int,
    color: bool,
    duplex: bool,
    cohort_id: int | None,
) -> PrintJob:
    """Return an exact retry or reject an idempotency-key/source collision.

    The open-source database key is the print request's durable idempotency
    identity. Silently accepting different physical options could print the
    wrong number or kind of pages while telling the caller its request worked.
    """

    requested = {
        "pages": pages,
        "copies": copies,
        "color": color,
        "duplex": duplex,
        "cohort": cohort_id,
    }
    recorded = {
        "pages": existing.pages,
        "copies": existing.copies,
        "color": existing.color,
        "duplex": existing.duplex,
        "cohort": existing.cohort_id,
    }
    conflicting_fields = sorted(name for name, value in requested.items() if recorded[name] != value)
    if conflicting_fields:
        raise ConflictException(
            _("An open print request already exists with different physical options."),
            code="print_idempotency_conflict",
            fields={"conflicting_fields": conflicting_fields},
        )
    return existing


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
    existing = _existing_open_job(
        branch_id=branch_id,
        source=source,
        source_id=source_id,
        payload_s3_key=payload_s3_key,
    )
    if existing is not None:
        return _assert_exact_print_retry(
            existing,
            pages=pages,
            copies=copies,
            color=color,
            duplex=duplex,
            cohort_id=cohort_id,
        )

    existing = _assert_within_quota(
        branch_id=branch_id,
        cohort_id=cohort_id,
        source=source,
        source_id=source_id,
        payload_s3_key=payload_s3_key,
        pages=pages,
        copies=copies,
        color=color,
        duplex=duplex,
    )
    if existing is not None:
        return existing

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
        existing = _existing_open_job(
            branch_id=branch_id,
            source=source,
            source_id=source_id,
            payload_s3_key=payload_s3_key,
        )
        if existing is None:
            raise
        return _assert_exact_print_retry(
            existing,
            pages=pages,
            copies=copies,
            color=color,
            duplex=duplex,
            cohort_id=cohort_id,
        )

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

    printers_qs = Printer.objects.select_for_update().filter(
        branch_id=job.branch_id,
        is_active=True,
    )
    # Do not deliberately route a job to a device that declares it cannot
    # perform the requested operation. An unassigned job remains available to
    # the branch agent's explicitly configured fallback device.
    if job.color:
        printers_qs = printers_qs.filter(capabilities__color=True)
    if job.duplex:
        printers_qs = printers_qs.filter(capabilities__duplex=True)
    # Lock the branch's eligible device rows before measuring load. Concurrent
    # claims then observe the assignment committed by the preceding claimant,
    # instead of both selecting the same apparently least-loaded printer.
    printers = list(printers_qs.order_by("id"))
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
    agent = _lock_active_agent(agent)
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
    job.lease_id = uuid.uuid4()
    job.last_heartbeat_at = now
    job.lease_expires_at = _lease_deadline(now)
    job.reconciliation_required_at = None
    job.reconciliation_reason = ""
    job.reconciliation_previous_status = ""
    job.next_attempt_at = None
    if job.printer_id is None:
        _assign_least_loaded_printer(job)
    job.save(
        update_fields=[
            "status",
            "agent",
            "claimed_at",
            "lease_id",
            "last_heartbeat_at",
            "lease_expires_at",
            "reconciliation_required_at",
            "reconciliation_reason",
            "reconciliation_previous_status",
            "next_attempt_at",
            "printer",
        ]
    )

    BranchAgent.objects.filter(pk=agent.pk).update(last_seen_at=now)
    return job


@transaction.atomic
def reject_invalid_claim(*, agent: BranchAgent, job_id: int) -> PrintJob:
    """Quarantine a claimed row whose domain source no longer matches its key.

    Invalid legacy/internal rows must not be retried: retrying would repeatedly expose
    the same signing attempt to branch agents and keep a poisoned job at the front of
    the queue.  The agent only supplies its authenticated identity and the already-
    claimed id; branch, ownership, and state are locked and rechecked here.
    """

    agent = _lock_active_agent(agent)
    try:
        job = PrintJob.objects.select_for_update().get(
            pk=job_id,
            branch_id=agent.branch_id,
            agent_id=agent.pk,
            status=PrintJob.Status.PICKED,
        )
    except PrintJob.DoesNotExist as exc:
        from core.exceptions import NotFoundException

        raise NotFoundException(_("Print job not found."), code="not_found") from exc

    now = timezone.now()
    job.status = PrintJob.Status.FAILED
    job.attempts = MAX_ATTEMPTS
    job.last_error = "invalid_print_source"
    job.finished_at = now
    job.next_attempt_at = None
    job.lease_id = None
    job.last_heartbeat_at = None
    job.lease_expires_at = None
    job.reconciliation_required_at = None
    job.reconciliation_reason = ""
    job.reconciliation_previous_status = ""
    job.save(
        update_fields=[
            "status",
            "attempts",
            "last_error",
            "finished_at",
            "next_attempt_at",
            "lease_id",
            "last_heartbeat_at",
            "lease_expires_at",
            "reconciliation_required_at",
            "reconciliation_reason",
            "reconciliation_previous_status",
        ]
    )
    _audit_job(job, action="print.job_rejected", agent_id=agent.pk)
    return job


# --------------------------------------------------------------------------- #
# Agent lease/status — never infer whether physical output happened
# --------------------------------------------------------------------------- #
def _locked_agent_job(*, agent: BranchAgent, job_id: int, lease_id: uuid.UUID) -> PrintJob:
    from core.exceptions import NotFoundException

    try:
        job = PrintJob.objects.select_for_update().get(
            pk=job_id,
            branch_id=agent.branch_id,
            agent_id=agent.pk,
        )
    except PrintJob.DoesNotExist as exc:
        raise NotFoundException(_("Print job not found."), code="not_found") from exc
    # A per-attempt lease is a second capability/version boundary. Return the
    # same 404 as a foreign job so a stale daemon cannot learn a later claim.
    if job.lease_id != lease_id:
        raise NotFoundException(_("Print job not found."), code="not_found")
    return job


def _apply_page_progress(job: PrintJob, pages_printed: int | None) -> None:
    if pages_printed is None:
        return
    maximum_pages = job.pages * job.copies
    if pages_printed < job.pages_printed:
        raise ValidationException(
            _("Printed pages cannot decrease."),
            code="validation_error",
            fields={"pages_printed": [_("Must be greater than or equal to the recorded count.")]},
        )
    if pages_printed > maximum_pages:
        raise ValidationException(
            _("Printed pages exceed the authorized job total."),
            code="validation_error",
            fields={"pages_printed": [_("Must not exceed the job page total.")]},
        )
    job.pages_printed = pages_printed


def _assert_no_output_before_printing(job: PrintJob, pages_printed: int | None) -> None:
    """Enforce the protocol boundary that makes a PICKED failure replay-safe."""

    if job.status == PrintJob.Status.PICKED and pages_printed not in (None, 0):
        raise ValidationException(
            _("Page progress requires an acknowledged printing state."),
            code="validation_error",
            fields={"pages_printed": [_("Report printing successfully before submitting physical output.")]},
        )


def _mark_reconciliation_required(
    job: PrintJob,
    *,
    now,
    reason: str = PrintJob.ReconciliationReason.LEASE_EXPIRED,
) -> PrintJob:
    """Quarantine one ambiguous attempt without releasing its unique/open key."""

    if job.status == PrintJob.Status.RECONCILIATION_REQUIRED:
        return job
    if job.status not in ACTIVE_LEASE_STATUSES:
        return job
    previous_status = job.status
    job.status = PrintJob.Status.RECONCILIATION_REQUIRED
    job.reconciliation_required_at = now
    job.reconciliation_reason = reason
    job.reconciliation_previous_status = previous_status
    job.next_attempt_at = None
    job.save(
        update_fields=[
            "status",
            "reconciliation_required_at",
            "reconciliation_reason",
            "reconciliation_previous_status",
            "next_attempt_at",
        ]
    )
    _audit_job(
        job,
        action="print.job_reconciliation_required",
        agent_id=job.agent_id,
        extra={"reason": reason},
    )
    return job


def _quarantine_if_expired(job: PrintJob, *, now) -> bool:
    if job.status == PrintJob.Status.RECONCILIATION_REQUIRED:
        return True
    if job.status not in ACTIVE_LEASE_STATUSES:
        return False
    if job.lease_expires_at is None or job.lease_expires_at <= now:
        _mark_reconciliation_required(job, now=now)
        return True
    return False


@transaction.atomic
def heartbeat_job(
    *,
    agent: BranchAgent,
    job_id: int,
    lease_id: uuid.UUID,
    pages_printed: int | None = None,
) -> PrintJob:
    """Renew an exact live claim without changing its physical state."""

    agent = _lock_active_agent(agent)
    now = timezone.now()
    job = _locked_agent_job(agent=agent, job_id=job_id, lease_id=lease_id)
    BranchAgent.objects.filter(pk=agent.pk).update(last_seen_at=now)
    if _quarantine_if_expired(job, now=now):
        return job
    if job.status not in ACTIVE_LEASE_STATUSES:
        raise ConflictException(
            _("This print job no longer has an active lease."),
            code="print_lease_inactive",
        )
    _assert_no_output_before_printing(job, pages_printed)
    _apply_page_progress(job, pages_printed)
    job.last_heartbeat_at = now
    job.lease_expires_at = _lease_deadline(now)
    job.save(update_fields=["last_heartbeat_at", "lease_expires_at", "pages_printed"])
    return job


@transaction.atomic
def update_job_status(
    *,
    agent: BranchAgent,
    job_id: int,
    lease_id: uuid.UUID,
    status: str,
    error: str = "",
    pages_printed: int | None = None,
) -> PrintJob:
    """Apply a report only while this exact physical-attempt lease is live.

    An expired attempt is durably quarantined and returned to the view, which
    emits stable 409 ``print_reconciliation_required``. It is never retried
    automatically because download/printing may already have happened.
    """

    agent = _lock_active_agent(agent)
    now = timezone.now()
    job = _locked_agent_job(agent=agent, job_id=job_id, lease_id=lease_id)
    BranchAgent.objects.filter(pk=agent.pk).update(last_seen_at=now)
    if _quarantine_if_expired(job, now=now):
        return job

    allowed = _ALLOWED_TRANSITIONS.get(job.status, set())
    if status not in allowed:
        raise ConflictException(
            _("Illegal print job status transition."),
            code="invalid_transition",
            fields={"from": job.status, "to": status},
        )

    _assert_no_output_before_printing(job, pages_printed)
    _apply_page_progress(job, pages_printed)

    if status == PrintJob.Status.PRINTING:
        job.status = PrintJob.Status.PRINTING
        job.last_heartbeat_at = now
        job.lease_expires_at = _lease_deadline(now)
        job.save(
            update_fields=[
                "status",
                "pages_printed",
                "last_heartbeat_at",
                "lease_expires_at",
            ]
        )
        return job

    reporting_agent_id = agent.pk
    if status == PrintJob.Status.DONE:
        total_pages = job.pages * job.copies
        if pages_printed is not None and pages_printed != total_pages:
            raise ValidationException(
                _("A completed print job must report the authorized page total."),
                code="validation_error",
                fields={"pages_printed": [_("Report the complete authorized page total.")]},
            )
        # DONE is itself the agent's assertion that the complete job printed.
        # Keep the counter authoritative even when an older agent omits the
        # optional progress field.
        job.pages_printed = total_pages
        job.status = PrintJob.Status.DONE
        job.finished_at = now
        _clear_current_lease(job)
        job.save(
            update_fields=[
                "status",
                "finished_at",
                "pages_printed",
                *_LEASE_CLEAR_FIELDS,
            ]
        )
        _audit_job(job, action="print.job_done", agent_id=reporting_agent_id)
        return job

    # Once PRINTING began (or any page progress was recorded), a failure can
    # mean partial physical output. Replaying the full job would duplicate
    # paper, so quarantine it exactly like a silent lease expiry. Only a
    # pre-output PICKED failure with zero progress is safe to retry.
    if job.status == PrintJob.Status.PRINTING or job.pages_printed > 0:
        job.last_error = (error or "")[:2000]
        job.save(update_fields=["last_error", "pages_printed"])
        return _mark_reconciliation_required(
            job,
            now=now,
            reason=PrintJob.ReconciliationReason.AGENT_REPORTED_FAILURE,
        )

    # status == FAILED from PICKED with no output — safe bounded retry.
    job.attempts += 1
    job.last_error = (error or "")[:2000]
    job.agent = None
    _clear_current_lease(job)

    if job.attempts < MAX_ATTEMPTS:
        backoff = (2**job.attempts) * RETRY_BACKOFF_SECONDS
        job.status = PrintJob.Status.QUEUED
        job.next_attempt_at = now + timedelta(seconds=backoff)
        job.pages_printed = 0
        job.printer = None
        job.claimed_at = None
        job.save(
            update_fields=[
                "status",
                "attempts",
                "last_error",
                "agent",
                "printer",
                "claimed_at",
                "next_attempt_at",
                "pages_printed",
                *_LEASE_CLEAR_FIELDS,
            ]
        )
        _audit_job(job, action="print.job_retry_scheduled", agent_id=reporting_agent_id)
        return job

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
            *_LEASE_CLEAR_FIELDS,
        ]
    )
    _audit_job(job, action="print.job_failed", agent_id=reporting_agent_id)
    _schedule_final_failure(job)
    return job


_LEASE_CLEAR_FIELDS = (
    "lease_id",
    "last_heartbeat_at",
    "lease_expires_at",
    "reconciliation_required_at",
    "reconciliation_reason",
    "reconciliation_previous_status",
)


def _clear_current_lease(job: PrintJob) -> None:
    job.lease_id = None
    job.last_heartbeat_at = None
    job.lease_expires_at = None
    job.reconciliation_required_at = None
    job.reconciliation_reason = ""
    job.reconciliation_previous_status = ""


def _schedule_final_failure(job: PrintJob) -> None:
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


def _validated_reconciliation_key(raw: str) -> str:
    if (
        not isinstance(raw, str)
        or raw != raw.strip()
        or not 16 <= len(raw) <= 128
        or any(ord(character) < 33 or ord(character) > 126 for character in raw)
    ):
        raise ValidationException(
            _("Idempotency-Key must contain 16 to 128 visible ASCII characters."),
            code="invalid_idempotency_key",
            fields={"Idempotency-Key": [_("Use 16 to 128 visible ASCII characters.")]},
        )
    return stable_hash(raw)


def _exact_reconciliation_retry(
    record: PrintJobReconciliation,
    *,
    job_id: int,
    outcome: str,
    evidence_reference: str,
) -> PrintJob:
    if (
        record.job_id != job_id
        or record.outcome != outcome
        or record.evidence_reference != evidence_reference
    ):
        raise ConflictException(
            _("This Idempotency-Key was already used for another reconciliation."),
            code="idempotency_key_reused",
        )
    return record.job


@transaction.atomic
def reconcile_print_job(
    *,
    job_id: int,
    expected_branch_id: int,
    actor: Any,
    outcome: str,
    evidence_reference: str,
    idempotency_key: str,
    actor_principal: Any = None,
) -> PrintJob:
    """Resolve one quarantined attempt from reviewed physical evidence.

    Only ``confirmed_not_printed`` can release the unique/open key for another
    delivery, and even then the bounded delivery-attempt policy still applies.
    Unknown outcomes are terminal and never generate another physical copy.
    """

    if outcome not in _RECONCILIATION_OUTCOMES:
        raise ValidationException(
            _("Unknown print reconciliation outcome."),
            code="validation_error",
            fields={"outcome": [_("Choose a supported reconciliation outcome.")]},
        )
    if getattr(actor, "pk", None) is None:
        raise ValidationException(
            _("An authenticated operator is required."),
            code="validation_error",
        )
    if not isinstance(evidence_reference, str):
        evidence_reference = ""
    evidence_reference = evidence_reference.strip()
    if (
        not evidence_reference
        or len(evidence_reference) > 200
        or any(unicodedata.category(character).startswith("C") for character in evidence_reference)
    ):
        raise ValidationException(
            _("A bounded, printable evidence reference is required."),
            code="validation_error",
            fields={"evidence_reference": [_("Use 1 to 200 printable characters.")]},
        )
    key_hash = _validated_reconciliation_key(idempotency_key)

    try:
        job = PrintJob.objects.select_for_update().get(
            pk=job_id,
            branch_id=expected_branch_id,
        )
    except PrintJob.DoesNotExist as exc:
        from core.exceptions import NotFoundException

        raise NotFoundException(_("Print job not found."), code="not_found") from exc

    existing = (
        PrintJobReconciliation.objects.select_related("job").filter(idempotency_key_hash=key_hash).first()
    )
    if existing is not None:
        return _exact_reconciliation_retry(
            existing,
            job_id=job_id,
            outcome=outcome,
            evidence_reference=evidence_reference,
        )
    if job.status != PrintJob.Status.RECONCILIATION_REQUIRED or job.lease_id is None:
        raise ConflictException(
            _("This print job does not require reconciliation."),
            code="print_reconciliation_not_required",
        )

    previous_status = job.reconciliation_previous_status
    if (
        previous_status not in ACTIVE_LEASE_STATUSES
        or job.reconciliation_reason not in PrintJob.ReconciliationReason.values
    ):
        # The database constraint makes this unreachable after migration, but a
        # corrupt/manual row must fail closed instead of inventing evidence.
        raise ConflictException(
            _("The print reconciliation state is incomplete."),
            code="print_reconciliation_state_invalid",
        )
    record_values = {
        "job": job,
        "branch_id": job.branch_id,
        "lease_id": job.lease_id,
        "previous_status": previous_status,
        "reason": job.reconciliation_reason,
        "outcome": outcome,
        "evidence_reference": evidence_reference,
        "pages_printed": job.pages_printed,
        "attempts": job.attempts,
        "agent_id_at_resolution": job.agent_id,
        "printer_id_at_resolution": job.printer_id,
        "resolved_by": actor if getattr(actor, "pk", None) else None,
        "idempotency_key_hash": key_hash,
    }
    try:
        with transaction.atomic():
            record = PrintJobReconciliation.objects.create(**record_values)
    except IntegrityError:
        # A different job can race on the globally unique operator key without
        # sharing this job lock. Recover after the savepoint and apply the same
        # exact-intent comparison as a normal retry.
        existing = (
            PrintJobReconciliation.objects.select_related("job").filter(idempotency_key_hash=key_hash).first()
        )
        if existing is None:
            raise
        return _exact_reconciliation_retry(
            existing,
            job_id=job_id,
            outcome=outcome,
            evidence_reference=evidence_reference,
        )

    now = timezone.now()
    reporting_agent_id = job.agent_id
    if outcome == PrintJobReconciliation.Outcome.CONFIRMED_PRINTED:
        job.status = PrintJob.Status.DONE
        job.pages_printed = job.pages * job.copies
        job.finished_at = now
        job.next_attempt_at = None
        _clear_current_lease(job)
    elif outcome == PrintJobReconciliation.Outcome.CONFIRMED_NOT_PRINTED:
        job.attempts += 1
        if job.attempts < MAX_ATTEMPTS:
            job.status = PrintJob.Status.QUEUED
            job.pages_printed = 0
            job.next_attempt_at = now
            job.finished_at = None
            job.claimed_at = None
        else:
            job.status = PrintJob.Status.FAILED
            job.last_error = "reconciled_not_printed_retry_exhausted"
            job.next_attempt_at = None
            job.finished_at = now
        job.agent = None
        job.printer = None
        _clear_current_lease(job)
    else:
        job.status = PrintJob.Status.FAILED
        job.last_error = "physical_output_unknown"
        job.next_attempt_at = None
        job.finished_at = now
        job.agent = None
        job.printer = None
        _clear_current_lease(job)

    job.save(
        update_fields=[
            "status",
            "pages_printed",
            "attempts",
            "last_error",
            "agent",
            "printer",
            "claimed_at",
            "finished_at",
            "next_attempt_at",
            *_LEASE_CLEAR_FIELDS,
        ]
    )
    _audit_job(
        job,
        action="print.job_reconciled",
        agent_id=reporting_agent_id,
        actor=actor,
        actor_principal=actor_principal,
        extra={
            "outcome": outcome,
            "reconciliation_id": record.pk,
        },
    )
    if job.status == PrintJob.Status.FAILED:
        _schedule_final_failure(job)
    return job


@transaction.atomic
def quarantine_stale_print_leases(*, batch_size: int, now=None) -> int:
    """Move at most ``batch_size`` expired active leases to manual review."""

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    now = now or timezone.now()
    jobs = list(
        PrintJob.objects.select_for_update(skip_locked=True)
        .filter(
            status__in=ACTIVE_LEASE_STATUSES,
            lease_expires_at__lte=now,
        )
        .order_by("lease_expires_at", "id")[:batch_size]
    )
    for job in jobs:
        _mark_reconciliation_required(job, now=now)
    return len(jobs)


def print_reconciliation_inventory(*, now=None) -> dict[str, int]:
    """Read-only, identifier-free monitoring counts for one tenant schema."""

    now = now or timezone.now()
    return {
        "stale_active_leases": PrintJob.objects.filter(
            status__in=ACTIVE_LEASE_STATUSES,
            lease_expires_at__lte=now,
        ).count(),
        "reconciliation_required": PrintJob.objects.filter(
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
        ).count(),
    }


def _audit_job(
    job: PrintJob,
    *,
    action: str,
    agent_id: int | None,
    actor: Any = None,
    actor_principal: Any = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a print audit row (TD-9). Lazy import — cross-app, never at module load."""
    from apps.audit.services import audit_log

    after = {
        "branch_id": job.branch_id,
        "source": job.source,
        "source_id": job.source_id,
        "pages": job.pages,
        "copies": job.copies,
        "status": job.status,
        "attempts": job.attempts,
        # BranchAgent is deliberately not a users.User and cannot populate
        # AuditLog.actor. Freeze the exact reporting device id in the event.
        "branch_agent_id": agent_id,
    }
    if extra:
        after.update(extra)
    audit_log(
        actor=actor if getattr(actor, "pk", None) else None,
        actor_principal=actor_principal,
        action=action,
        resource_type="printing.PrintJob",
        resource_id=job.pk,
        after=after,
    )
