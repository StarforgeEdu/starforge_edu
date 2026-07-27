"""Procurement service — scoped reads + the branch lookup, wrapping the preserved
`create_purchase_order` domain fn (which totals the items and raises the A-1 request)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.org.models import Branch
from apps.procurement.dto.purchase_order_dto import CreatePurchaseOrderDTO
from apps.procurement.interfaces.repositories import IPurchaseOrderRepository
from apps.procurement.interfaces.services import IPurchaseOrderService
from apps.procurement.models import PurchaseOrder
from apps.procurement.services import create_purchase_order


class PurchaseOrderService(IPurchaseOrderService):
    def __init__(self, repository: IPurchaseOrderRepository) -> None:
        self.repository = repository

    def scoped_list(self, *, is_unscoped: bool, user) -> QuerySet[PurchaseOrder]:
        return self.repository.scoped(is_unscoped=is_unscoped, user=user)

    def get_visible(self, *, is_unscoped: bool, user, pk: int) -> PurchaseOrder | None:
        return self.repository.get_scoped(is_unscoped=is_unscoped, user=user, pk=pk)

    def get_branch(self, *, branch_id: int) -> Branch | None:
        return self.repository.get_branch(branch_id=branch_id)

    def create(self, data: CreatePurchaseOrderDTO, *, requested_by, branch) -> PurchaseOrder:
        return create_purchase_order(
            requested_by=requested_by,
            supplier=data.supplier,
            title=data.title,
            items=[
                {"description": ln.description, "quantity": ln.quantity, "unit_price_uzs": ln.unit_price_uzs}
                for ln in data.items
            ],
            description=data.description,
            branch=branch,
        )
