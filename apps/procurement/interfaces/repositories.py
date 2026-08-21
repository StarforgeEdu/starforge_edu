"""Procurement-domain repository port.

A purchase order is visible to: a director/superuser and any finance handler (holds
approvals:approve or approvals:disburse) — they see EVERY PO (they approve/disburse the
money); a plain requester sees only the POs they raised (request__requested_by), mirroring
the approvals-queue scoping. Out-of-scope rows are filtered OUT (a detail 404s).
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.org.models import Branch
from apps.procurement.models import PurchaseOrder
from core.interfaces import IBaseRepository


class IPurchaseOrderRepository(IBaseRepository[PurchaseOrder]):
    def scoped(self, *, is_unscoped: bool, user, branch_ids: set[int]) -> QuerySet[PurchaseOrder]:
        raise NotImplementedError

    def get_scoped(self, *, is_unscoped: bool, user, branch_ids: set[int], pk: int) -> PurchaseOrder | None:
        raise NotImplementedError

    def get_branch(self, *, branch_id: int) -> Branch | None:
        raise NotImplementedError
