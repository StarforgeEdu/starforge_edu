"""ORM-backed forms repository (role-scoped reads)."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.forms.interfaces.repositories import IFormRepository
from apps.forms.models import Form
from core.repositories import BaseRepository


class FormRepository(BaseRepository[Form], IFormRepository):
    model = Form

    def get_queryset(self) -> QuerySet[Form]:
        return Form.objects.select_related("branch", "created_by").prefetch_related("fields")

    def scoped(self, *, user, is_unscoped: bool, can_write: bool, branch_ids: set[int]) -> QuerySet[Form]:
        qs = self.get_queryset()
        if is_unscoped:
            return qs  # the director sees the whole centre
        if can_write:
            # A builder manages only their own branches' forms (+ anything they made) —
            # never another branch's responses/summaries (the isolation gate: every
            # detail action resolves through this queryset).
            return qs.filter(Q(created_by=user) | Q(branch_id__in=branch_ids))
        # Responders: PUBLISHED forms in their branch, plus centre-wide (branch null).
        return qs.filter(status=Form.Status.PUBLISHED).filter(
            Q(branch_id__in=branch_ids) | Q(branch__isnull=True)
        )

    def get_scoped(
        self, *, user, is_unscoped: bool, can_write: bool, branch_ids: set[int], pk: int
    ) -> Form | None:
        return (
            self.scoped(user=user, is_unscoped=is_unscoped, can_write=can_write, branch_ids=branch_ids)
            .filter(pk=pk)
            .first()
        )
