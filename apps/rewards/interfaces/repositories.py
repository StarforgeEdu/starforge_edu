"""Reward-domain repository ports."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.rewards.models import RewardGrant, RewardType
from core.interfaces import IBaseRepository


class IRewardTypeRepository(IBaseRepository[RewardType]):
    ...


class IRewardGrantRepository(IBaseRepository[RewardGrant]):
    def all_grants(self) -> QuerySet[RewardGrant]:
        """Every grant (manager view), relations eager-loaded."""
        raise NotImplementedError

    def owned_by(self, user) -> QuerySet[RewardGrant]:
        """Grants the user received OR granted (staff view)."""
        raise NotImplementedError

    def received_by(self, user) -> QuerySet[RewardGrant]:
        """Grants the user received (the `mine` wall)."""
        raise NotImplementedError

    def get_visible(self, *, user, is_manager: bool, pk: int) -> RewardGrant | None:
        """One grant by pk visible to the caller (a manager sees any; a staff member
        only their own), or None → 404 (no existence leak across staff)."""
        raise NotImplementedError
