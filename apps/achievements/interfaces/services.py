"""Achievement-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.achievements.dto.achievement_dto import CreateAchievementDTO, GrantAchievementDTO
from apps.achievements.models import Achievement, AchievementGrant


class IAchievementService(ABC):
    @abstractmethod
    def scoped_list(
        self, *, user, is_unscoped: bool, can_write: bool, can_approve: bool, branch_ids: set[int]
    ) -> QuerySet[Achievement]: ...

    @abstractmethod
    def get_visible(
        self, *, user, is_unscoped: bool, can_write: bool, can_approve: bool, branch_ids: set[int], pk: int
    ) -> Achievement | None: ...

    @abstractmethod
    def create(
        self, data: CreateAchievementDTO, *, creator, can_approve: bool, is_scoped: bool, branch_ids: set[int]
    ) -> Achievement: ...

    @abstractmethod
    def decide(self, *, achievement_id: int, approve: bool, actor) -> Achievement: ...

    @abstractmethod
    def grant(
        self, achievement: Achievement, data: GrantAchievementDTO, *, granted_by, student=None
    ) -> AchievementGrant: ...

    @abstractmethod
    def resolve_student(self, student_id: int): ...

    @abstractmethod
    def wall_for(self, user) -> QuerySet[AchievementGrant]: ...

    @abstractmethod
    def grants_of(self, achievement: Achievement) -> QuerySet[AchievementGrant]: ...
