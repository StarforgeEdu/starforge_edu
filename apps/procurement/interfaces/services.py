"""Procurement-domain service port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.org.models import Branch
from apps.procurement.dto.purchase_order_dto import CreatePurchaseOrderDTO
from apps.procurement.models import PurchaseOrder


class IPurchaseOrderService(ABC):
    @abstractmethod
    def scoped_list(self, *, is_unscoped: bool, user) -> QuerySet[PurchaseOrder]: ...

    @abstractmethod
    def get_visible(self, *, is_unscoped: bool, user, pk: int) -> PurchaseOrder | None: ...

    @abstractmethod
    def get_branch(self, *, branch_id: int) -> Branch | None: ...

    @abstractmethod
    def create(self, data: CreatePurchaseOrderDTO, *, requested_by, branch) -> PurchaseOrder: ...
