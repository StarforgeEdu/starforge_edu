"""RewardTypeService — the center's reward catalog (create + update; no delete)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.rewards.dto.reward_dto import RewardTypeCreateDTO
from apps.rewards.interfaces.repositories import IRewardTypeRepository
from apps.rewards.interfaces.services import IRewardTypeService
from apps.rewards.models import RewardType
from core.exceptions import ValidationException

_SCALARS = ("name", "is_cash", "default_amount_uzs", "description", "is_active")


def _assert_name_free(name: str, *, exclude_pk: int | None = None) -> None:
    """400 (field error) if the reward-type name is taken — restores the
    UniqueValidator the old ModelSerializer applied (name is unique)."""
    qs = RewardType.objects.filter(name=name)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise ValidationException(
            _("A reward type with this name already exists."),
            code="validation_error",
            fields={"name": ["A reward type with this name already exists."]},
        )


class RewardTypeService(IRewardTypeService):
    def __init__(self, types: IRewardTypeRepository) -> None:
        self._types = types

    def list(self) -> QuerySet[RewardType]:
        return self._types.get_queryset()

    def get(self, type_id: int) -> RewardType | None:
        return self._types.get_by_id(type_id)

    def create(self, data: RewardTypeCreateDTO, *, creator) -> RewardType:
        from apps.rewards.services import create_reward_type

        _assert_name_free(data.name)
        return create_reward_type(
            creator=creator,
            name=data.name,
            is_cash=data.is_cash,
            default_amount_uzs=data.default_amount_uzs,
            description=data.description,
            is_active=data.is_active,
        )

    def update(self, reward_type: RewardType, changes: dict[str, Any]) -> RewardType:
        if "name" in changes:
            _assert_name_free(changes["name"], exclude_pk=reward_type.pk)
        for field in _SCALARS:
            if field in changes:
                setattr(reward_type, field, changes[field])
        reward_type.save()
        return reward_type
