"""Plain dict presenters for the billing platform API (off DRF).

Replace the DRF ModelSerializers with explicit ``*_to_dict`` functions. Money
fields render as fixed 2-dp strings (DRF DecimalField parity); datetimes/dates
via ISO strings.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apps.billing.models import AiUsageCharge, Plan, Subscription, UsageSnapshot

_CENT = Decimal("0.01")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _money(value: Any) -> str | None:
    return str(Decimal(value).quantize(_CENT)) if value is not None else None


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "code": plan.code,
        "name": plan.name,
        "max_students": plan.max_students,
        "max_branches": plan.max_branches,
        "ai_tokens_month": plan.ai_tokens_month,
        "storage_gb": plan.storage_gb,
        "price_uzs": _money(plan.price_uzs),
        "ai_overage_price_per_1k_uzs": _money(plan.ai_overage_price_per_1k_uzs),
        "is_active": plan.is_active,
    }


def subscription_to_dict(sub: Subscription) -> dict[str, Any]:
    return {
        "id": sub.id,
        "center": sub.center_id,
        "center_name": sub.center.name,
        "plan": plan_to_dict(sub.plan),
        "status": sub.status,
        "current_period_start": _iso(sub.current_period_start),
        "current_period_end": _iso(sub.current_period_end),
        "created_at": _iso(sub.created_at),
        "updated_at": _iso(sub.updated_at),
    }


def usage_snapshot_to_dict(snap: UsageSnapshot) -> dict[str, Any]:
    return {
        "id": snap.id,
        "center": snap.center_id,
        "date": _iso(snap.date),
        "students_count": snap.students_count,
        "storage_bytes": snap.storage_bytes,
        "ai_tokens_used": snap.ai_tokens_used,
        "created_at": _iso(snap.created_at),
    }


def ai_usage_charge_to_dict(charge: AiUsageCharge) -> dict[str, Any]:
    return {
        "id": charge.id,
        "center": charge.center_id,
        "center_name": charge.center.name,
        "period": _iso(charge.period),
        "included_tokens": charge.included_tokens,
        "used_tokens": charge.used_tokens,
        "overage_tokens": charge.overage_tokens,
        "rate_per_1k_uzs": _money(charge.rate_per_1k_uzs),
        "amount_uzs": _money(charge.amount_uzs),
        "cost_microusd": charge.cost_microusd,
        "created_at": _iso(charge.created_at),
        "updated_at": _iso(charge.updated_at),
    }
