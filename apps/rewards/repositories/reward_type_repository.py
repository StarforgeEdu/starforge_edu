"""ORM-backed reward-type repository."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.rewards.interfaces.repositories import IRewardTypeRepository
from apps.rewards.models import RewardType
from core.repositories import BaseRepository


class RewardTypeRepository(BaseRepository[RewardType], IRewardTypeRepository):
    model = RewardType

    def get_queryset(self) -> QuerySet[RewardType]:
        return RewardType.objects.select_related("created_by")
