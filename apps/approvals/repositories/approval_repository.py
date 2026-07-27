"""ORM-backed approvals repositories (reads scoped via the preserved selectors)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.approvals import selectors
from apps.approvals.interfaces.repositories import (
    IApprovalRequestRepository,
    ILedgerEntryRepository,
)
from apps.approvals.models import ApprovalRequest, LedgerEntry
from core.repositories import BaseRepository


class ApprovalRequestRepository(BaseRepository[ApprovalRequest], IApprovalRequestRepository):
    model = ApprovalRequest

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[ApprovalRequest]:
        return selectors.scoped_requests(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> ApprovalRequest | None:
        return selectors.scoped_requests(user=user, roles=roles).filter(pk=pk).first()


class LedgerEntryRepository(BaseRepository[LedgerEntry], ILedgerEntryRepository):
    model = LedgerEntry

    def list_entries(self) -> QuerySet[LedgerEntry]:
        return LedgerEntry.objects.select_related("branch", "payment_method", "created_by").all()
