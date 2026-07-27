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
        self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int]
    ) -> QuerySet[CoverRequest]:
        qs = self.get_queryset()
        if is_unscoped:
            return qs
        if is_manager:
            return qs.filter(branch_id__in=branch_ids)  # managers see their branch's requests
        # a teacher sees: their own requests, claimable pool requests in their branch,
        # and requests assigned to them.
        return qs.filter(
            Q(requester=user)
            | (Q(pool=True, status=CoverRequest.Status.OPEN) & Q(branch_id__in=branch_ids))
            | Q(cover_teacher__user=user)
        )

    def get_scoped(
        self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int], pk: int
    ) -> CoverRequest | None:
        return (
            self.scoped(user=user, is_unscoped=is_unscoped, is_manager=is_manager, branch_ids=branch_ids)
            .filter(pk=pk)
            .first()
        )
