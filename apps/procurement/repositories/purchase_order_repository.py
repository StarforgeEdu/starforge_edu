"""ORM-backed procurement repository — role/ownership-scoped reads."""

from __future__ import annotations

from django.db.models import QuerySet

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

    def scoped(self, *, is_unscoped: bool, user) -> QuerySet[PurchaseOrder]:
        qs = self._base()
        if is_unscoped:
            return qs
        return qs.filter(request__requested_by=user)  # a plain requester sees only their own

    def get_scoped(self, *, is_unscoped: bool, user, pk: int) -> PurchaseOrder | None:
        return self.scoped(is_unscoped=is_unscoped, user=user).filter(pk=pk).first()

    def get_branch(self, *, branch_id: int) -> Branch | None:
        return Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
