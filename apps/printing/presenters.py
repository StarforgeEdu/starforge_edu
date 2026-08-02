"""Printing response presenters.

Staff responses intentionally expose operational state, never storage locations,
internal failure text, token material, or arbitrary device-connection metadata.
"""

from __future__ import annotations

from apps.printing.models import BranchAgent, Printer, PrintJob, PrintJobReconciliation

_SAFE_PAPER_SIZES = {
    *(f"A{number}" for number in range(11)),
    *(f"B{number}" for number in range(11)),
    *(f"C{number}" for number in range(11)),
    "EXECUTIVE",
    "FOLIO",
    "LEDGER",
    "LEGAL",
    "LETTER",
    "STATEMENT",
    "TABLOID",
}


def print_job_to_dict(job: PrintJob) -> dict:
    """Leadership-safe print-work state.

    ``payload_s3_key`` is an object location/capability input and ``last_error`` can
    contain device addresses, filesystem paths, document names, or driver diagnostics.
    Neither belongs in an ordinary list/detail response. The agent claim endpoint reads
    the key directly from the model to mint its short-lived download URL.
    """
    return {
        "id": job.id,
        "branch": job.branch_id,
        "printer": job.printer_id,
        "agent": job.agent_id,
        "status": job.status,
        "source": job.source,
        "source_id": job.source_id,
        "pages": job.pages,
        "copies": job.copies,
        "color": job.color,
        "duplex": job.duplex,
        "cohort_id": job.cohort_id,
        "requested_by": job.requested_by_id,
        "attempts": job.attempts,
        "next_attempt_at": job.next_attempt_at.isoformat() if job.next_attempt_at else None,
        "pages_printed": job.pages_printed,
        "created_at": job.created_at.isoformat(),
        "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "last_heartbeat_at": job.last_heartbeat_at.isoformat() if job.last_heartbeat_at else None,
        "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
        "reconciliation_required_at": (
            job.reconciliation_required_at.isoformat() if job.reconciliation_required_at else None
        ),
        "reconciliation_reason": job.reconciliation_reason or None,
        "reconciliation_previous_status": job.reconciliation_previous_status or None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def agent_print_job_to_dict(job: PrintJob) -> dict:
    """Device-safe print instructions and acknowledgement state.

    A branch agent already knows its tenant, branch, and own identity. Internal
    domain identifiers (source_id/cohort), the requesting account, and staff-facing
    timestamps do not help it print a document and would unnecessarily widen the
    data available to a compromised device token.
    """

    return {
        "id": job.id,
        "printer": job.printer_id,
        "status": job.status,
        "source": job.source,
        "pages": job.pages,
        "copies": job.copies,
        "color": job.color,
        "duplex": job.duplex,
        "attempts": job.attempts,
        "next_attempt_at": job.next_attempt_at.isoformat() if job.next_attempt_at else None,
        "pages_printed": job.pages_printed,
        "lease_id": str(job.lease_id) if job.lease_id else None,
        "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
    }


def print_job_reconciliation_to_dict(record: PrintJobReconciliation) -> dict:
    """Operator-visible evidence without raw idempotency or device lease tokens."""

    return {
        "id": record.id,
        "job": record.job_id,
        "outcome": record.outcome,
        "evidence_reference": record.evidence_reference,
        "previous_status": record.previous_status,
        "reason": record.reason,
        "pages_printed": record.pages_printed,
        "attempts": record.attempts,
        "agent": record.agent_id_at_resolution,
        "printer": record.printer_id_at_resolution,
        "resolved_by": record.resolved_by_id,
        "resolved_at": record.resolved_at.isoformat(),
    }


def printer_to_dict(printer: Printer) -> dict:
    return {
        "id": printer.id,
        "branch": printer.branch_id,
        "name": printer.name,
        "model_name": printer.model_name,
        "capabilities": _public_capabilities(printer.capabilities),
        "is_active": printer.is_active,
        "created_at": printer.created_at.isoformat(),
        "updated_at": printer.updated_at.isoformat(),
    }


def branch_agent_to_dict(agent: BranchAgent) -> dict:
    # token_hash is intentionally NEVER serialized.
    return {
        "id": agent.id,
        "branch": agent.branch_id,
        "name": agent.name,
        "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
        "revoked_at": agent.revoked_at.isoformat() if agent.revoked_at else None,
        "created_at": agent.created_at.isoformat(),
    }


def branch_agent_created_to_dict(agent: BranchAgent, raw_token: str) -> dict:
    """The one-time creation response — includes the raw token (shown a single time)."""
    return {
        "id": agent.id,
        "branch": agent.branch_id,
        "name": agent.name,
        "token": raw_token,
        "created_at": agent.created_at.isoformat(),
    }


def _public_capabilities(raw: object) -> dict:
    """Whitelist documented, non-secret equipment capabilities.

    ``Printer.capabilities`` predates a typed schema and can contain arbitrary JSON.
    Echoing it would turn a convenient metadata field into a connection-secret leak.
    The public contract documents color/duplex/paper only, so fail closed to those
    values and conservative primitive types.
    """
    if not isinstance(raw, dict):
        return {}

    safe: dict[str, object] = {}
    for name in ("color", "duplex"):
        value = raw.get(name)
        if isinstance(value, bool):
            safe[name] = value

    paper = raw.get("paper")
    if isinstance(paper, list):
        sizes: list[str] = []
        for value in paper:
            if not isinstance(value, str):
                continue
            normalized = value.strip().upper()
            if normalized in _SAFE_PAPER_SIZES and normalized not in sizes:
                sizes.append(normalized)
        if sizes:
            safe["paper"] = sizes
    return safe
