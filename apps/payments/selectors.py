"""Payments read selectors (D3-B-10). Reads only; eager-loaded + scoped."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Q, QuerySet

from apps.payments.models import Payment
from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES


def payments_qs() -> QuerySet[Payment]:
    return (
        Payment.objects.filter(attribution_status__in=ATTRIBUTED_SCOPE_STATUSES)
        .select_related(
            "payer",
            "cashier_shift",
            "branch_at_payment",
            "department_at_payment",
        )
        .order_by("-created_at")
    )


def payments_for_branches(queryset: QuerySet[Payment], *, branch_ids: set[int]) -> QuerySet[Payment]:
    """Limit the staff payment log to transactions belonging to their branches.

    Historical ownership is captured directly on every supported write. Never
    reconstruct it from current student placement, an editable account string,
    or an allocation that might have been created after a transfer.
    """
    return queryset.filter(
        attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
        branch_at_payment_id__in=branch_ids,
    )


def payments_for_scopes(
    queryset: QuerySet[Payment],
    *,
    scope_pairs: set[tuple[int, int | None]],
) -> QuerySet[Payment]:
    """Limit payments to exact branch/department membership boundaries."""
    visible = Q(pk__in=[])
    for branch_id, department_id in scope_pairs:
        if department_id is None:
            visible |= Q(branch_at_payment_id=branch_id)
        else:
            visible |= Q(
                branch_at_payment_id=branch_id,
                department_at_payment_id=department_id,
            )
    return queryset.filter(
        visible,
        attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
    )


def reconciliation(
    *,
    on: date,
    scope_pairs: set[tuple[int, int | None]] | None = None,
) -> dict[str, Any]:
    """Payments completed on ``on`` vs the amount finance allocated against them.

    Mismatch = a completed payment whose allocated total != its amount. Finance's
    ``PaymentAllocation`` carries a soft ``payment_id`` (BigInteger, not an FK —
    Lane A decision), so we sum it via a lazy query and tolerate finance absent.
    """
    completed_qs = Payment.objects.filter(
        status=Payment.Status.COMPLETED,
        paid_at__date=on,
        attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
    )
    if scope_pairs is not None:
        completed_qs = payments_for_scopes(completed_qs, scope_pairs=scope_pairs)
    completed = list(completed_qs.values("id", "amount_uzs", "provider", "allocation_status"))
    payment_ids = [p["id"] for p in completed]
    from apps.finance.models import PaymentAllocation

    allocated: dict[int, Decimal] = {}
    rows = PaymentAllocation.objects.filter(payment_id__in=payment_ids).values_list(
        "payment_id", "amount_uzs"
    )
    for pid, amt in rows:
        allocated[pid] = allocated.get(pid, Decimal("0")) + (amt or Decimal("0"))

    total_paid = sum((p["amount_uzs"] for p in completed), Decimal("0"))
    total_allocated = sum(allocated.values(), Decimal("0"))
    mismatches = [
        {
            "payment_id": p["id"],
            "amount_uzs": str(p["amount_uzs"]),
            "allocated_uzs": str(allocated.get(p["id"], Decimal("0"))),
            "allocation_status": p["allocation_status"],
        }
        for p in completed
        if allocated.get(p["id"], Decimal("0")) != p["amount_uzs"]
    ]
    by_provider: dict[str, Decimal] = {}
    for p in completed:
        by_provider[p["provider"]] = by_provider.get(p["provider"], Decimal("0")) + p["amount_uzs"]
    return {
        "date": on.isoformat(),
        "total_paid_uzs": str(total_paid),
        "total_allocated_uzs": str(total_allocated),
        "by_provider": {k: str(v) for k, v in by_provider.items()},
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }
