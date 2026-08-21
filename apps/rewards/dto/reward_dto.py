"""Reward-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RewardTypeCreateDTO:
    name: str
    is_cash: bool = False
    default_amount_uzs: Decimal | None = None
    description: str = ""
    is_active: bool = True


@dataclass(frozen=True)
class GrantRewardDTO:
    reward_type_id: int
    recipient_id: int
    amount_uzs: Decimal | None = None
    reason: str = ""
