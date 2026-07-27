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


def invoice_to_dict(inv: Invoice) -> dict[str, Any]:
    return {
        "id": inv.id,
        "number": inv.number,
        "student": inv.student_id,
        "student_name": inv.student.get_full_name() if inv.student_id else "",
        "cohort": inv.cohort_id,
        "cohort_name": inv.cohort.name if inv.cohort else None,
        "fee_schedule": inv.fee_schedule_id,
        "fee_schedule_name": inv.fee_schedule.name if inv.fee_schedule else None,
        "period": inv.period,
        "status": inv.status,
        "issue_date": _iso(inv.issue_date),
        "due_date": _iso(inv.due_date),
        "currency": inv.currency,
        "total_uzs": _money(inv.total_uzs),
        "fx_rate_usd": _rate(inv.fx_rate_usd),
        "fx_source": inv.fx_source,
        "total_usd": _money(inv.total_usd),
        "created_by": inv.created_by_id,
        "created_by_name": inv.created_by.get_full_name() if inv.created_by else None,
        "created_at": _iso(inv.created_at),
        "lines": [invoice_line_to_dict(line) for line in inv.lines.all()],
        "allocations": [payment_allocation_to_dict(a) for a in inv.allocations.all()],
    }


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
    return {
        "id": e.id,
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
        "opening_cash_uzs": _money(s.opening_cash_uzs),
        "closing_cash_uzs": _money(s.closing_cash_uzs),
        "discrepancy_uzs": _money(s.discrepancy_uzs),
        "notes": s.notes,
    }


def outstanding_to_dict(*, student_id: int, outstanding_uzs: Any, invoices: Any) -> dict[str, Any]:
    return {
        "student": student_id,
        "outstanding_uzs": _money(outstanding_uzs),
        "invoices": [invoice_to_dict(inv) for inv in invoices],
    }
