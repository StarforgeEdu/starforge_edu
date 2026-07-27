"""ORM-backed achievement repository (role-scoped reads)."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.achievements.interfaces.repositories import IAchievementRepository
from apps.achievements.models import Achievement
from core.repositories import BaseRepository


class AchievementRepository(BaseRepository[Achievement], IAchievementRepository):
    model = Achievement

    def get_queryset(self) -> QuerySet[Achievement]:
        return Achievement.objects.select_related("cohort", "branch", "created_by")

    def scoped(
        self, *, user, is_unscoped: bool, can_write: bool, can_approve: bool, branch_ids: set[int]
    ) -> QuerySet[Achievement]:
        qs = self.get_queryset()
        if is_unscoped:
            return qs  # the director manages the whole centre
        if can_write or can_approve:
            # Keep write and approval capabilities independent: dynamic permission
            # overrides can grant achievements:approve without achievements:write.
            # Such an approver must still see the pending-global queue that their
            # approve/reject endpoint authorizes them to action.
            visible = Q(pk__in=[])
            if can_write:
                visible |= (
                    Q(created_by=user)
                    | Q(branch_id__in=branch_ids)
                    | (Q(branch__isnull=True) & Q(status=Achievement.Status.ACTIVE))
                )
            if can_approve:
                visible |= Q(branch__isnull=True) & Q(status=Achievement.Status.PENDING)
            return qs.filter(visible)
        return qs.filter(status=Achievement.Status.ACTIVE)  # students/parents: the live catalogue

    def get_scoped(
        self, *, user, is_unscoped: bool, can_write: bool, can_approve: bool, branch_ids: set[int], pk: int
    ) -> Achievement | None:
        return (
            self.scoped(
                user=user,
                is_unscoped=is_unscoped,
                can_write=can_write,
                can_approve=can_approve,
                branch_ids=branch_ids,
            )
            .filter(pk=pk)
            .first()
        )
