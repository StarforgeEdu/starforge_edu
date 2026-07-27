"""Printing response presenters (the DRF serializer output shapes)."""

from __future__ import annotations

from apps.printing.models import BranchAgent, Printer, PrintJob


def print_job_to_dict(job: PrintJob) -> dict:
    return {
        "id": job.id,
        "branch": job.branch_id,
        "printer": job.printer_id,
        "agent": job.agent_id,
        "status": job.status,
        "source": job.source,
        "source_id": job.source_id,
        "payload_s3_key": job.payload_s3_key,
        "pages": job.pages,
        "copies": job.copies,
        "color": job.color,
        "duplex": job.duplex,
        "cohort_id": job.cohort_id,
        "requested_by": job.requested_by_id,
        "attempts": job.attempts,
        "next_attempt_at": job.next_attempt_at.isoformat() if job.next_attempt_at else None,
        "pages_printed": job.pages_printed,
        "last_error": job.last_error,
        "created_at": job.created_at.isoformat(),
        "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def printer_to_dict(printer: Printer) -> dict:
    return {
        "id": printer.id,
        "branch": printer.branch_id,
        "name": printer.name,
        "model_name": printer.model_name,
        "capabilities": printer.capabilities,
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
