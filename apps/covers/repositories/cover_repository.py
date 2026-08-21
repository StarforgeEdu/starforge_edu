"""ORM-backed cover-request repository (role-scoped reads)."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.covers.interfaces.repositories import ICoverRepository
from apps.covers.models import CoverRequest
from core.repositories import BaseRepository


class CoverRepository(BaseRepository[CoverRequest], ICoverRepository):
    model = CoverRequest

    def get_queryset(self) -> QuerySet[CoverRequest]:
        return CoverRequest.objects.select_related(
            "lesson", "requester", "cover_teacher", "branch", "decided_by"
        )

    def scoped(
        self,
        *,
        user,
        is_unscoped: bool,
        is_manager: bool,
        manager_branch_ids: set[int],
        teacher_branch_ids: set[int],
    ) -> QuerySet[CoverRequest]:
        qs = self.get_queryset()
        if is_unscoped:
            return qs
        visible = Q(requester=user) | Q(cover_teacher__user=user)
        if is_manager:
            visible |= Q(branch_id__in=manager_branch_ids)
        visible |= Q(pool=True, status=CoverRequest.Status.OPEN) & Q(branch_id__in=teacher_branch_ids)
        return qs.filter(visible)

    def get_scoped(
        self,
        *,
        user,
        is_unscoped: bool,
        is_manager: bool,
        manager_branch_ids: set[int],
        teacher_branch_ids: set[int],
        pk: int,
    ) -> CoverRequest | None:
        return (
            self.scoped(
                user=user,
                is_unscoped=is_unscoped,
                is_manager=is_manager,
                manager_branch_ids=manager_branch_ids,
                teacher_branch_ids=teacher_branch_ids,
            )
            .filter(pk=pk)
            .first()
        )
