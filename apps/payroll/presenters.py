"""Permission-safe payroll response presenters."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.utils import timezone

from apps.payroll.models import (
    PayrollAdjustment,
    PayrollAdjustmentEvent,
    PayrollExport,
    PayrollLineItem,
    PayrollPayslip,
    PayrollPeriod,
    PayrollPeriodEvent,
    PayrollReconciliation,
)


def _money(value: Any) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def period_to_dict(period: PayrollPeriod) -> dict[str, Any]:
    return {
        "id": period.pk,
        "label": period.label,
        "branch": period.branch_id,
        "branch_name": period.branch.name,
        "department": period.department_id,
        "department_name": period.department.name if period.department else None,
        "period_start": period.period_start.isoformat(),
        "period_end": period.period_end.isoformat(),
        "pay_date": _iso(period.pay_date),
        "currency": period.currency,
        "organization_timezone": period.organization_timezone,
        "status": period.status,
        "correction_of": period.correction_of_id,
        "correction_reason": period.correction_reason,
        "line_count": period.line_count,
        "base_total_uzs": _money(period.base_total_uzs),
        "bonus_total_uzs": _money(period.bonus_total_uzs),
        "deduction_total_uzs": _money(period.deduction_total_uzs),
        "net_total_uzs": _money(period.net_total_uzs),
        "paid_total_uzs": _money(period.paid_total_uzs),
        "outstanding_total_uzs": _money(period.net_total_uzs - period.paid_total_uzs),
        "created_by": period.created_by_id,
        "created_principal": {
            "kind": period.created_principal_kind,
            "id": period.created_principal_id,
        },
        "run_by": period.run_by_id,
        "run_principal": (
            {"kind": period.run_principal_kind, "id": period.run_principal_id}
            if period.run_principal_id
            else None
        ),
        "approved_by": period.approved_by_id,
        "approved_principal": (
            {"kind": period.approved_principal_kind, "id": period.approved_principal_id}
            if period.approved_principal_id
            else None
        ),
        "rejected_by": period.rejected_by_id,
        "rejected_principal": (
            {"kind": period.rejected_principal_kind, "id": period.rejected_principal_id}
            if period.rejected_principal_id
            else None
        ),
        "decision_note": period.decision_note,
        "version": period.version,
        "frozen_at": _iso(period.frozen_at),
        "decided_at": _iso(period.decided_at),
        "created_at": _iso(period.created_at),
        "updated_at": _iso(period.updated_at),
    }


def preview_to_dict(result: dict[str, Any]) -> dict[str, Any]:
    period = result["period"]
    return {
        "generated_at": _iso(timezone.now()),
        "period": period.pk,
        "scope": {
            "branch": {"id": period.branch_id, "name": period.branch.name},
            "department": (
                {"id": period.department_id, "name": period.department.name} if period.department else None
            ),
        },
        "window": {
            "period_start": period.period_start.isoformat(),
            "period_end": period.period_end.isoformat(),
            "timezone": period.organization_timezone,
        },
        "currency": period.currency,
        "valid": result["valid"],
        "teacher_count": result["teacher_count"],
        "line_count": len(result["rows"]),
        "base_total_uzs": _money(result["base_total_uzs"]),
        "bonus_total_uzs": _money(result["bonus_total_uzs"]),
        "deduction_total_uzs": _money(result["deduction_total_uzs"]),
        "net_total_uzs": _money(result["net_total_uzs"]),
        "errors": result["errors"],
        "lines": [
            {
                "teacher": row["teacher"].pk,
                "teacher_code": row["teacher"].username or f"teacher-{row['teacher'].pk}",
                "teacher_name": (
                    row["teacher"].get_full_name()
                    or row["teacher"].username
                    or f"Teacher {row['teacher'].pk}"
                ),
                "payout_method": row["policy"].method,
                "base_amount_uzs": _money(row["base_amount_uzs"]),
                "bonus_amount_uzs": _money(row["bonus_amount_uzs"]),
                "deduction_amount_uzs": _money(row["deduction_amount_uzs"]),
                "net_amount_uzs": _money(row["net_amount_uzs"]),
                "currency": period.currency,
                "calculation": row["breakdown"],
                "approved_adjustment_count": len(row["adjustment_ids"]),
            }
            for row in result["rows"]
        ],
    }


def line_to_dict(line: PayrollLineItem) -> dict[str, Any]:
    paid = Decimal(getattr(line, "paid_amount_uzs", 0) or 0)
    outstanding = Decimal(getattr(line, "outstanding_amount_uzs", line.net_amount_uzs - paid) or 0)
    return {
        "id": line.pk,
        "period": line.period_id,
        "payslip": line.payslip.pk,
        "payslip_number": line.payslip.document_number,
        "teacher": line.teacher_id,
        "teacher_code": line.teacher_code_snapshot,
        "teacher_name": line.teacher_name_snapshot,
        "branch_at_run": line.branch_at_run_id,
        "branch_at_run_name": line.branch_at_run.name,
        "department_at_run": line.department_at_run_id,
        "department_at_run_name": line.department_at_run.name if line.department_at_run else None,
        "payout_method": line.payout_method_snapshot,
        "payout_policy": line.payout_policy_snapshot,
        "calculation": line.calculation_breakdown,
        "base_amount_uzs": _money(line.base_amount_uzs),
        "bonus_amount_uzs": _money(line.bonus_amount_uzs),
        "deduction_amount_uzs": _money(line.deduction_amount_uzs),
        "net_amount_uzs": _money(line.net_amount_uzs),
        "paid_amount_uzs": _money(paid),
        "outstanding_amount_uzs": _money(outstanding),
        "payment_state": ("unpaid" if paid == 0 else "paid" if outstanding == 0 else "partial"),
        "currency": line.currency,
        "created_at": _iso(line.created_at),
    }


def disbursement_to_dict(line: PayrollLineItem) -> dict[str, Any]:
    """Minimum payment instruction visible to a compensation disburser.

    Policy, base/bonus/deduction composition, and unrelated payroll totals stay
    behind ``compensation:read``.  A cashier receives only the immutable payee,
    payslip, scope, due date, and remaining amount needed to record payment.
    """

    paid = Decimal(getattr(line, "paid_amount_uzs", 0) or 0)
    outstanding = Decimal(getattr(line, "outstanding_amount_uzs", line.net_amount_uzs - paid) or 0)
    return {
        "line_item": line.pk,
        "period": line.period_id,
        "period_label": line.period.label,
        "pay_date": _iso(line.period.pay_date),
        "payslip": line.payslip.pk,
        "payslip_number": line.payslip.document_number,
        "teacher": line.teacher_id,
        "teacher_code": line.teacher_code_snapshot,
        "teacher_name": line.teacher_name_snapshot,
        "branch_at_run": line.branch_at_run_id,
        "branch_at_run_name": line.branch_at_run.name,
        "department_at_run": line.department_at_run_id,
        "department_at_run_name": line.department_at_run.name if line.department_at_run else None,
        "amount_uzs": _money(line.net_amount_uzs),
        "paid_amount_uzs": _money(paid),
        "outstanding_amount_uzs": _money(outstanding),
        "currency": line.currency,
    }


def payslip_to_dict(payslip: PayrollPayslip) -> dict[str, Any]:
    line = payslip.line_item
    return {
        "id": payslip.pk,
        "document_number": payslip.document_number,
        "period": line.period_id,
        "period_status": line.period.status,
        "branch_at_run": line.branch_at_run_id,
        "branch_at_run_name": line.branch_at_run.name,
        "department_at_run": line.department_at_run_id,
        "department_at_run_name": line.department_at_run.name if line.department_at_run else None,
        "snapshot": payslip.snapshot,
        "generated_at": _iso(payslip.generated_at),
    }


def adjustment_to_dict(adjustment: PayrollAdjustment) -> dict[str, Any]:
    return {
        "id": adjustment.pk,
        "teacher": adjustment.teacher_id,
        "teacher_code": adjustment.teacher.username or f"teacher-{adjustment.teacher_id}",
        "teacher_name": (
            adjustment.teacher.get_full_name()
            or adjustment.teacher.username
            or f"Teacher {adjustment.teacher_id}"
        ),
        "branch": adjustment.branch_id,
        "branch_name": adjustment.branch.name,
        "department": adjustment.department_id,
        "department_name": adjustment.department.name if adjustment.department else None,
        "kind": adjustment.kind,
        "amount_uzs": _money(adjustment.amount_uzs),
        "currency": adjustment.currency,
        "effective_period_start": adjustment.effective_period_start.isoformat(),
        "effective_period_end": adjustment.effective_period_end.isoformat(),
        "reason": adjustment.reason,
        "state": adjustment.state,
        "created_by": adjustment.created_by_id,
        "created_principal": {
            "kind": adjustment.created_principal_kind,
            "id": adjustment.created_principal_id,
        },
        "decided_by": adjustment.decided_by_id,
        "decided_principal": (
            {"kind": adjustment.decided_principal_kind, "id": adjustment.decided_principal_id}
            if adjustment.decided_principal_id
            else None
        ),
        "decided_at": _iso(adjustment.decided_at),
        "decision_reason": adjustment.decision_reason,
        "applied_line": adjustment.applied_line_id,
        "created_at": _iso(adjustment.created_at),
    }


def reconciliation_to_dict(row: PayrollReconciliation) -> dict[str, Any]:
    return {
        "id": row.pk,
        "line_item": row.line_item_id,
        "kind": row.kind,
        "reverses": row.reverses_id,
        "amount_uzs": _money(row.amount_uzs),
        "currency": row.currency,
        "payment_method": row.payment_method_id,
        "payment_method_name": row.payment_method.name,
        "external_reference": row.external_reference,
        "paid_at": _iso(row.paid_at),
        "reason": row.reason,
        "ledger_entry": row.ledger_entry_id,
        "recorded_by": row.recorded_by_id,
        "recorded_principal": {
            "kind": row.recorded_principal_kind,
            "id": row.recorded_principal_id,
        },
        "created_at": _iso(row.created_at),
    }


def period_event_to_dict(row: PayrollPeriodEvent) -> dict[str, Any]:
    return {
        "id": row.pk,
        "period": row.period_id,
        "action": row.action,
        "actor": row.actor_id,
        "actor_principal": {
            "kind": row.actor_principal_kind,
            "id": row.actor_principal_id,
        },
        "note": row.note,
        "created_at": _iso(row.created_at),
    }


def adjustment_event_to_dict(row: PayrollAdjustmentEvent) -> dict[str, Any]:
    return {
        "id": row.pk,
        "adjustment": row.adjustment_id,
        "action": row.action,
        "actor": row.actor_id,
        "actor_principal": {
            "kind": row.actor_principal_kind,
            "id": row.actor_principal_id,
        },
        "note": row.note,
        "created_at": _iso(row.created_at),
    }


def export_to_dict(export: PayrollExport, *, include_download: bool = False) -> dict[str, Any]:
    from apps.payroll.services import DOWNLOAD_TTL_SECONDS, presign_export

    # A collection read must not mint a batch of signed bearer URLs.  Only the
    # purpose-built export detail operation issues one short-lived download.
    url = presign_export(export) if include_download else None
    return {
        "id": export.pk,
        "period": export.period_id,
        "format": export.format,
        "filters": export.filters,
        "status": export.status,
        "file_bytes": export.file_bytes,
        "error_code": export.error_code,
        "download_url": url,
        "download_expires_in": DOWNLOAD_TTL_SECONDS if url else None,
        "created_at": _iso(export.created_at),
        "started_at": _iso(export.started_at),
        "finished_at": _iso(export.finished_at),
    }
