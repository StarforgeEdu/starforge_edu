"""Plain dict presenters for the finance app (off DRF).

Replace the DRF read serializers. Money renders as fixed-precision strings
(2dp for UZS amounts, 4dp for the FX rate) matching the old DecimalField output;
datetimes/dates via ISO strings.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apps.finance.models import (
    CashierShift,
    Discount,
    Expense,
    FeeSchedule,
    Invoice,
    InvoiceLine,
    PaymentAllocation,
    PaymentMethod,
    PaymentPlan,
    PaymentPlanInstallment,
    Refund,
)

_2DP = Decimal("0.01")
_4DP = Decimal("0.0001")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _money(value: Any) -> str | None:
    return str(Decimal(value).quantize(_2DP)) if value is not None else None


def _rate(value: Any) -> str | None:
    return str(Decimal(value).quantize(_4DP)) if value is not None else None


def fee_schedule_to_dict(fs: FeeSchedule) -> dict[str, Any]:
    # `cohort` is a nullable FK (center-wide default when null) — surface its readable
    # name alongside the id. The list selector select_relateds cohort (no N+1).
    return {
        "id": fs.id,
        "name": fs.name,
        "cohort": fs.cohort_id,
        "cohort_name": fs.cohort.name if fs.cohort else None,
        "amount_uzs": _money(fs.amount_uzs),
        "billing_period": fs.billing_period,
        "due_day_of_month": fs.due_day_of_month,
        "is_active": fs.is_active,
        "created_at": _iso(fs.created_at),
    }


def invoice_line_to_dict(line: InvoiceLine) -> dict[str, Any]:
    return {
        "id": line.id,
        "description": line.description,
        "line_type": line.line_type,
        "quantity": _money(line.quantity),
        "unit_price_uzs": _money(line.unit_price_uzs),
        "amount_uzs": _money(line.amount_uzs),
    }


def payment_allocation_to_dict(a: PaymentAllocation) -> dict[str, Any]:
    return {
        "id": a.id,
        "payment_id": a.payment_id,
        "amount_uzs": _money(a.amount_uzs),
        "created_at": _iso(a.created_at),
    }


def _invoice_outstanding(inv: Invoice, *, allocated_uzs: Decimal) -> Decimal:
    # Drafts are not yet receivable; void/paid invoices no longer belong in
    # outstanding debt even if historical data is imperfect.  Open statuses are
    # clamped at zero so corrupt legacy over-allocation is never shown as credit.
    if inv.status not in (
        Invoice.Status.ISSUED,
        Invoice.Status.PARTIALLY_PAID,
        Invoice.Status.OVERDUE,
    ):
        return Decimal("0.00")
    return max(inv.total_uzs - allocated_uzs, Decimal("0.00"))


def _invoice_summary_payload(inv: Invoice, *, allocated_uzs: Decimal) -> dict[str, Any]:
    return {
        "id": inv.id,
        "number": inv.number,
        "student": inv.student_id,
        "student_name": inv.student.get_full_name() if inv.student_id else "",
        "cohort": inv.cohort_id,
        "cohort_name": inv.cohort.name if inv.cohort else None,
        "branch_at_issue": inv.branch_at_issue_id,
        "branch_at_issue_name": inv.branch_at_issue.name if inv.branch_at_issue else None,
        "department_at_issue": inv.department_at_issue_id,
        "department_at_issue_name": (inv.department_at_issue.name if inv.department_at_issue else None),
        "attribution_status": inv.attribution_status,
        "fee_schedule": inv.fee_schedule_id,
        "fee_schedule_name": inv.fee_schedule.name if inv.fee_schedule else None,
        "period": inv.period,
        "status": inv.status,
        "issue_date": _iso(inv.issue_date),
        "due_date": _iso(inv.due_date),
        "currency": inv.currency,
        "total_uzs": _money(inv.total_uzs),
        "outstanding_uzs": _money(_invoice_outstanding(inv, allocated_uzs=allocated_uzs)),
        "fx_rate_usd": _rate(inv.fx_rate_usd),
        "fx_source": inv.fx_source,
        "total_usd": _money(inv.total_usd),
        "created_by": inv.created_by_id,
        "created_by_name": inv.created_by.get_full_name() if inv.created_by else None,
        "created_at": _iso(inv.created_at),
    }


def invoice_list_to_dict(inv: Invoice) -> dict[str, Any]:
    """Lightweight invoice-register row: scalar summary fields only.

    ``scoped_invoice_summaries`` annotates the exact allocation total in the same
    SQL query; intentionally do not access either reverse collection here.
    """
    allocated_uzs = Decimal(getattr(inv, "allocated_uzs", Decimal("0.00")))
    return _invoice_summary_payload(inv, allocated_uzs=allocated_uzs)


def invoice_to_dict(inv: Invoice) -> dict[str, Any]:
    """Full invoice detail including its line items and payment allocations."""
    allocations = list(inv.allocations.all())
    allocated_uzs = sum((allocation.amount_uzs for allocation in allocations), Decimal("0.00"))
    payload = _invoice_summary_payload(inv, allocated_uzs=allocated_uzs)
    payload.update(
        lines=[invoice_line_to_dict(line) for line in inv.lines.all()],
        allocations=[payment_allocation_to_dict(allocation) for allocation in allocations],
    )
    return payload


def discount_to_dict(d: Discount) -> dict[str, Any]:
    # `student` is a non-null FK; `approved_by` is a nullable User FK. The list
    # selector select_relateds student__user + approved_by (no N+1).
    return {
        "id": d.id,
        "student": d.student_id,
        "student_name": d.student.get_full_name(),
        "discount_type": d.discount_type,
        "percent": _money(d.percent),
        "fixed_amount_uzs": _money(d.fixed_amount_uzs),
        "valid_from": _iso(d.valid_from),
        "valid_until": _iso(d.valid_until),
        "approved_by": d.approved_by_id,
        "approved_by_name": d.approved_by.get_full_name() if d.approved_by else None,
        "is_active": d.is_active,
        "created_at": _iso(d.created_at),
    }


def installment_to_dict(inst: PaymentPlanInstallment) -> dict[str, Any]:
    return {
        "id": inst.id,
        "due_date": _iso(inst.due_date),
        "amount_uzs": _money(inst.amount_uzs),
        "status": inst.status,
    }


def payment_plan_to_dict(plan: PaymentPlan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "invoice": plan.invoice_id,
        "installments": [installment_to_dict(i) for i in plan.installments.all()],
        "created_at": _iso(plan.created_at),
    }


def payment_method_to_dict(pm: PaymentMethod) -> dict[str, Any]:
    return {
        "id": pm.id,
        "name": pm.name,
        "slug": pm.slug,
        "is_active": pm.is_active,
    }


def expense_to_dict(e: Expense) -> dict[str, Any]:
    # Each bare FK keeps a readable companion (branch/payment_method names + the
    # three User actors). ExpenseRepository.query select_relateds all five (no N+1).
    approval = e.approval_request if e.approval_request_id else None
    return {
        "id": e.id,
        "approval_request": e.approval_request_id,
        "ledger_entry": approval.ledger_entry_id if approval is not None else None,
        "branch": e.branch_id,
        "branch_name": e.branch.name,
        "category": e.category,
        "description": e.description,
        "amount_uzs": _money(e.amount_uzs),
        "status": e.status,
        "payment_method": e.payment_method_id,
        "payment_method_name": e.payment_method.name if e.payment_method else None,
        "reject_reason": e.reject_reason,
        "created_by": e.created_by_id,
        "created_by_name": e.created_by.get_full_name() if e.created_by else None,
        "approved_by": e.approved_by_id,
        "approved_by_name": e.approved_by.get_full_name() if e.approved_by else None,
        "paid_by": e.paid_by_id,
        "paid_by_name": e.paid_by.get_full_name() if e.paid_by else None,
        "created_at": _iso(e.created_at),
        "approved_at": _iso(e.approved_at),
        "paid_at": _iso(e.paid_at),
    }


def cashier_shift_to_dict(s: CashierShift) -> dict[str, Any]:
    # `cashier` (User) + `branch` are non-null FKs; the repository query
    # select_relateds both (no N+1).
    return {
        "id": s.id,
        "cashier": s.cashier_id,
        "cashier_name": s.cashier.get_full_name(),
        "branch": s.branch_id,
        "branch_name": s.branch.name,
        "status": s.status,
        "opened_at": _iso(s.opened_at),
        "closed_at": _iso(s.closed_at),
        "closed_by": s.closed_by_id,
        "opening_cash_uzs": _money(s.opening_cash_uzs),
        "closing_cash_uzs": _money(s.closing_cash_uzs),
        "discrepancy_uzs": _money(s.discrepancy_uzs),
        "notes": s.notes,
    }


def refund_to_dict(refund: Refund) -> dict[str, Any]:
    return {
        "id": refund.pk,
        "invoice": refund.invoice_id,
        "payment_id": refund.payment_id,
        "amount_uzs": _money(refund.amount_uzs),
        "reason": refund.reason,
        "state": refund.state,
        "provider": refund.provider,
        "provider_refund_id": refund.provider_refund_id,
        "provider_confirmed_at": _iso(refund.provider_confirmed_at),
        "requested_by": refund.requested_by_id,
        "approved_by": refund.approved_by_id,
        "ledger_entry": refund.ledger_entry_id,
        "created_at": _iso(refund.created_at),
        "updated_at": _iso(refund.updated_at),
    }


def outstanding_to_dict(*, student_id: int, outstanding_uzs: Any, invoices: Any) -> dict[str, Any]:
    return {
        "student": student_id,
        "outstanding_uzs": _money(outstanding_uzs),
        "invoices": [invoice_to_dict(inv) for inv in invoices],
    }
