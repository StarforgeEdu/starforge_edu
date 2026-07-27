"""Approvals response presenters (the DRF serializer output shapes)."""

from __future__ import annotations

from decimal import Decimal

from apps.approvals.models import ApprovalRequest, LedgerEntry


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _money(value) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.01")))


def approval_request_to_dict(req: ApprovalRequest) -> dict:
    return {
        "id": req.id,
        "kind": req.kind,
        "branch": req.branch_id,
        "requested_by": req.requested_by_id,
        "title": req.title,
        "description": req.description,
        "amount_uzs": _money(req.amount_uzs),
        "payload": req.payload,
        "status": req.status,
        "decided_by": req.decided_by_id,
        "decided_at": _iso(req.decided_at),
        "decision_note": req.decision_note,
        "disbursed_by": req.disbursed_by_id,
        "disbursed_at": _iso(req.disbursed_at),
        "payment_method": req.payment_method_id,
        "ledger_entry": req.ledger_entry_id,
        "created_at": _iso(req.created_at),
    }


def ledger_entry_to_dict(entry: LedgerEntry) -> dict:
    return {
        "id": entry.id,
        "direction": entry.direction,
        "entry_type": entry.entry_type,
        "amount_uzs": _money(entry.amount_uzs),
        "branch": entry.branch_id,
        "party_label": entry.party_label,
        "payment_method": entry.payment_method_id,
        "source_kind": entry.source_kind,
        "source_id": entry.source_id,
        "note": entry.note,
        "created_by": entry.created_by_id,
        "created_at": _iso(entry.created_at),
    }
