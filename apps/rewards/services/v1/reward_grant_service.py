"""RewardGrantService — grant a reward (cash routes through A-1) + scoped reads."""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.rewards.dto.reward_dto import GrantRewardDTO
from apps.rewards.interfaces.repositories import IRewardGrantRepository
from apps.rewards.interfaces.services import IRewardGrantService
from apps.rewards.models import RewardGrant
from core.exceptions import ValidationException


class RewardGrantService(IRewardGrantService):
    def __init__(self, grants: IRewardGrantRepository) -> None:
        self._grants = grants

    def list_all(self) -> QuerySet[RewardGrant]:
        return self._grants.all_grants()

    def received_by(self, user) -> QuerySet[RewardGrant]:
        return self._grants.received_by(user)

    def get_visible(self, *, user, is_manager: bool, pk: int) -> RewardGrant | None:
        return self._grants.get_visible(user=user, is_manager=is_manager, pk=pk)

    def grant(self, data: GrantRewardDTO, *, granted_by) -> RewardGrant:
        from apps.rewards.services import grant_reward

        return grant_reward(
            reward_type=self._resolve_type(data.reward_type_id),
            recipient=self._resolve_staff_recipient(data.recipient_id),
            granted_by=granted_by,
            amount_uzs=data.amount_uzs,
            reason=data.reason,
        )

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _resolve_type(type_id: int):
        from apps.rewards.models import RewardType

        rt = RewardType.objects.filter(pk=type_id).first()
        if rt is None:
            raise ValidationException(
                _("Invalid reward type."),
                code="validation_error",
                fields={"reward_type": ["Not found."]},
            )
        return rt

    @staticmethod
    def _resolve_staff_recipient(recipient_id: int):
        from apps.access.models import AccountType
        from apps.users.models import User
        from core.permissions import role_memberships_for_account_kinds

        # Rewards go to STAFF only (never students/parents) — mirrors the old
        # GrantRewardSerializer recipient queryset.
        staff_memberships = role_memberships_for_account_kinds(
            (AccountType.AccountKind.STAFF, AccountType.AccountKind.TEACHER)
        )
        recipient = (
            User.objects.filter(
                pk=recipient_id,
                is_active=True,
                role_memberships__in=staff_memberships,
            )
            .distinct()
            .first()
        )
        if recipient is None:
            raise ValidationException(
                _("Invalid recipient."),
                code="validation_error",
                fields={"recipient": ["Not a valid staff recipient in this center."]},
            )
        return recipient
