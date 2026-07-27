"""Loans response presenters (the DRF LoanSerializer / LoanRepaymentSerializer shape).

A loan is an ApprovalRequest(kind="loan") plus its repayment standing (repaid /
outstanding / settled), computed from the queryset annotation (no N+1 on list) with a
per-object fallback.
"""

from __future__ import annotations

from decimal import Decimal

from apps.approvals.models import ApprovalRequest
from apps.loans.models import LoanRepayment
from apps.loans.services import repaid_total

_TWO_PLACES = Decimal("0.01")


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(_TWO_PLACES))


def _repaid(loan: ApprovalRequest) -> Decimal:
    # Prefer the queryset annotation (no N+1 on list); fall back to a query.
    annotated = getattr(loan, "repaid_uzs_annotated", None)
    return annotated if annotated is not None else repaid_total(loan)


def loan_to_dict(loan: ApprovalRequest) -> dict:
    repaid = _repaid(loan)
    # outstanding/settled only make sense once the money has gone out.
    outstanding: Decimal | None = None
    if loan.status == ApprovalRequest.Status.DISBURSED and loan.amount_uzs is not None:
        outstanding = loan.amount_uzs - repaid
    return {
        "id": loan.id,
        "kind": loan.kind,
        "branch": loan.branch_id,
        "requested_by": loan.requested_by_id,
        "title": loan.title,
        "description": loan.description,
        "amount_uzs": _money(loan.amount_uzs),
        "payload": loan.payload,
        "status": loan.status,
        "decided_by": loan.decided_by_id,
        "decided_at": loan.decided_at.isoformat() if loan.decided_at else None,
        "disbursed_by": loan.disbursed_by_id,
        "disbursed_at": loan.disbursed_at.isoformat() if loan.disbursed_at else None,
        "ledger_entry": loan.ledger_entry_id,
        # repaid is quantized to 2dp ("0.00", not "0"); outstanding mirrors the old
        # serializer (str of amount - repaid, both already 2dp).
        "repaid_uzs": str(repaid.quantize(_TWO_PLACES)),
        "outstanding_uzs": str(outstanding) if outstanding is not None else None,
        "settled": bool(outstanding is not None and outstanding <= 0),
        "created_at": loan.created_at.isoformat(),
    }


def repayment_to_dict(row: LoanRepayment) -> dict:
    return {
        "id": row.id,
        "loan": row.loan_id,
        "amount_uzs": _money(row.amount_uzs),
        "branch": row.branch_id,
        "payment_method": row.payment_method_id,
        "ledger_entry": row.ledger_entry_id,
        "recorded_by": row.recorded_by_id,
        "note": row.note,
        "created_at": row.created_at.isoformat(),
    }
