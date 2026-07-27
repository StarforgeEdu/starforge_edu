"""Reward-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.rewards.dto.reward_dto import GrantRewardDTO, RewardTypeCreateDTO
from apps.rewards.models import RewardGrant, RewardType


class IRewardTypeService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[RewardType]: ...

    @abstractmethod
    def get(self, type_id: int) -> RewardType | None: ...

    @abstractmethod
    def create(self, data: RewardTypeCreateDTO, *, creator) -> RewardType: ...

    @abstractmethod
    def update(self, reward_type: RewardType, changes: dict[str, Any]) -> RewardType: ...


class IRewardGrantService(ABC):
    @abstractmethod
    def list_all(self) -> QuerySet[RewardGrant]: ...

    @abstractmethod
    def received_by(self, user) -> QuerySet[RewardGrant]: ...

    @abstractmethod
    def get_visible(self, *, user, is_manager: bool, pk: int) -> RewardGrant | None: ...

    @abstractmethod
    def grant(self, data: GrantRewardDTO, *, granted_by) -> RewardGrant: ...
