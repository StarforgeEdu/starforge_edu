"""Payments read selectors (D3-B-10). Reads only; eager-loaded + scoped."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import QuerySet

from apps.payments.models import Payment


def payments_qs() -> QuerySet[Payment]:
    return Payment.objects.select_related("payer", "cashier_shift").order_by("-created_at")


def payment_with_attempts(payment_id: int) -> Payment:
    return (
        Payment.objects.select_related("payer", "fiscal_receipt")
        .prefetch_related("attempts")
        .get(pk=payment_id)
    )


def reconciliation(*, on: date) -> dict[str, Any]:
    """Payments completed on ``on`` vs the amount finance allocated against them.

    Mismatch = a completed payment whose allocated total != its amount. Finance's
    ``PaymentAllocation`` carries a soft ``payment_id`` (BigInteger, not an FK —
    Lane A decision), so we sum it via a lazy query and tolerate finance absent.
    """
    completed = list(
        Payment.objects.filter(status=Payment.Status.COMPLETED, paid_at__date=on).values(
            "id", "amount_uzs", "provider", "allocation_status"
        )
    )
    payment_ids = [p["id"] for p in completed]
    allocated: dict[int, Decimal] = {}
    try:
        from apps.finance.models import PaymentAllocation

        rows = PaymentAllocation.objects.filter(payment_id__in=payment_ids).values_list(
            "payment_id", "amount_uzs"
        )
        for pid, amt in rows:
            allocated[pid] = allocated.get(pid, Decimal("0")) + (amt or Decimal("0"))
    except Exception:
        allocated = {}

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
