"""ORM-backed procurement repository — role/ownership-scoped reads."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.org.models import Branch
from apps.procurement.interfaces.repositories import IPurchaseOrderRepository
from apps.procurement.models import PurchaseOrder
from core.repositories import BaseRepository


class PurchaseOrderRepository(BaseRepository[PurchaseOrder], IPurchaseOrderRepository):
    model = PurchaseOrder

    def _base(self) -> QuerySet[PurchaseOrder]:
        return PurchaseOrder.objects.select_related("request", "branch", "created_by").prefetch_related(
            "items"
        )

    def scoped(self, *, is_unscoped: bool, user, branch_ids: set[int]) -> QuerySet[PurchaseOrder]:
        qs = self._base()
        if is_unscoped:
            return qs
        return qs.filter(
            Q(request__requested_by=user) | Q(branch_id__in=branch_ids)
        )  # requesters keep their own rows; handlers see only their permission branches

    def get_scoped(self, *, is_unscoped: bool, user, branch_ids: set[int], pk: int) -> PurchaseOrder | None:
        return self.scoped(is_unscoped=is_unscoped, user=user, branch_ids=branch_ids).filter(pk=pk).first()

    def get_branch(self, *, branch_id: int) -> Branch | None:
        return Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
