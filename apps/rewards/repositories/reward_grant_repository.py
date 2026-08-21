"""ORM-backed reward-grant repository (role-scoped: manager-all / staff-own)."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.rewards.interfaces.repositories import IRewardGrantRepository
from apps.rewards.models import RewardGrant
from core.repositories import BaseRepository


class RewardGrantRepository(BaseRepository[RewardGrant], IRewardGrantRepository):
    model = RewardGrant

    def get_queryset(self) -> QuerySet[RewardGrant]:
        return RewardGrant.objects.select_related(
            "reward_type", "recipient", "granted_by", "approval_request"
        )

    def all_grants(self) -> QuerySet[RewardGrant]:
        return self.get_queryset()

    def owned_by(self, user) -> QuerySet[RewardGrant]:
        return self.get_queryset().filter(Q(recipient=user) | Q(granted_by=user))

    def received_by(self, user) -> QuerySet[RewardGrant]:
        return self.get_queryset().filter(recipient=user)

    def get_visible(self, *, user, is_manager: bool, pk: int) -> RewardGrant | None:
        base = self.all_grants() if is_manager else self.owned_by(user)
        return base.filter(pk=pk).first()
