"""Achievement-domain repository ports.

Read scoping is role-based: a director/superuser sees the whole centre; a staff
member who may write sees their own creations, their branch's achievements, and the
active centre-wide catalogue (plus, if they may approve, the pending-global queue);
everyone else (students/parents) sees only the active catalogue.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.achievements.models import Achievement, AchievementGrant
from core.interfaces import IBaseRepository


class IAchievementRepository(IBaseRepository[Achievement]):
    def scoped(
        self, *, user, is_unscoped: bool, can_write: bool, can_approve: bool, branch_ids: set[int]
    ) -> QuerySet[Achievement]:
        raise NotImplementedError

    def get_scoped(
        self, *, user, is_unscoped: bool, can_write: bool, can_approve: bool, branch_ids: set[int], pk: int
    ) -> Achievement | None:
        raise NotImplementedError


class IAchievementGrantRepository(IBaseRepository[AchievementGrant]):
    def wall_for(self, user) -> QuerySet[AchievementGrant]:
        """The signed-in student's granted achievements, or a parent's children's."""
        raise NotImplementedError

    def grants_of(self, achievement: Achievement) -> QuerySet[AchievementGrant]:
        """Every grant of one achievement (a staff-only roster)."""
        raise NotImplementedError
