"""Finance generator (D4-LB-3): invoice totals + paid/outstanding by status.

Params: optional ``date_from`` / ``date_to`` (filter on ``issue_date``). Finance
is director/accountant only (enforced by the report's ``allowed_roles`` + the
matrix); no teacher cohort scoping applies. Money stays exact Decimal, serialized
as 2dp strings in the data dict (renderers do no arithmetic).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Count, Sum

from apps.finance.models import Invoice
from apps.reports.generators.base import (
    ReportGenerator,
    assert_report_generation_authorized,
    is_full_scope,
    staff_report_scope_q,
)
from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES

_ZERO = Decimal("0")
_OPEN = (Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _money(value) -> str:
    return str((Decimal(value or _ZERO)).quantize(Decimal("0.01")))


class FinanceGenerator(ReportGenerator):
    key = "finance"
    title = "Finance report"
    template_base = "finance"

    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
        assert_report_generation_authorized(report_key=self.key, user=user, roles=roles)
        qs = Invoice.objects.filter(attribution_status__in=ATTRIBUTED_SCOPE_STATUSES)
        if params.get("branch_id"):
            qs = qs.filter(branch_at_issue_id=params["branch_id"])
        if not is_full_scope(user=user, roles=roles, report_key=self.key):
            qs = qs.filter(
                staff_report_scope_q(
                    report_key=self.key,
                    roles=roles,
                    user=user,
                    branch_field="branch_at_issue_id",
                    department_field="department_at_issue_id",
                )
            )
        date_from = _parse_date(params.get("date_from"))
        date_to = _parse_date(params.get("date_to"))
        if date_from:
            qs = qs.filter(issue_date__gte=date_from)
        if date_to:
            qs = qs.filter(issue_date__lte=date_to)

        # Per-status aggregate: count + billed total. One grouped query.
        status_rows = list(
            qs.values("status").annotate(count=Count("id"), billed=Sum("total_uzs")).order_by("status")
        )
        rows = [
            {
                "status": r["status"],
                "count": r["count"],
                "billed_uzs": _money(r["billed"]),
            }
            for r in status_rows
        ]

        # Outstanding = open invoices billed minus their allocations (2 aggregates).
        open_qs = qs.filter(status__in=_OPEN)
        billed_open = open_qs.aggregate(s=Sum("total_uzs"))["s"] or _ZERO
        allocated_open = open_qs.aggregate(s=Sum("allocations__amount_uzs"))["s"] or _ZERO
        outstanding = (Decimal(billed_open) - Decimal(allocated_open)).quantize(Decimal("0.01"))

        total_billed = qs.aggregate(s=Sum("total_uzs"))["s"] or _ZERO
        total_allocated = qs.aggregate(s=Sum("allocations__amount_uzs"))["s"] or _ZERO

        return {
            "columns": ["status", "count", "billed_uzs"],
            "rows": rows,
            "total_invoices": qs.count(),
            "total_billed_uzs": _money(total_billed),
            "total_collected_uzs": _money(total_allocated),
            "outstanding_uzs": str(outstanding),
        }
