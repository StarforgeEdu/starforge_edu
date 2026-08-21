"""Reward-domain presenters — plain dict mappers (replace the DRF serializers)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apps.rewards.models import RewardGrant, RewardType

_CENTS = Decimal("0.01")


def _money(value) -> str | None:
    # Quantize to 2 dp so the string matches DRF's DecimalField output ("500000.00").
    return str(value.quantize(_CENTS)) if value is not None else None


def reward_type_to_dict(t: RewardType) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "is_cash": t.is_cash,
        "default_amount_uzs": _money(t.default_amount_uzs),
        "description": t.description,
        "is_active": t.is_active,
        "created_by": t.created_by_id,
        "created_at": t.created_at.isoformat(),
    }


def reward_grant_to_dict(g: RewardGrant) -> dict[str, Any]:
    approval = g.approval_request if g.approval_request_id else None
    return {
        "id": g.id,
        "reward_type": g.reward_type_id,
        "reward_type_detail": reward_type_to_dict(g.reward_type),
        "recipient": g.recipient_id,
        "amount_uzs": _money(g.amount_uzs),
        "reason": g.reason,
        "granted_by": g.granted_by_id,
        "approval_request": g.approval_request_id,
        "approval_status": approval.status if approval else None,
        "granted_at": g.granted_at.isoformat(),
    }
