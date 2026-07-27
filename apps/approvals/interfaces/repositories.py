"""Approvals-domain repository ports."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.approvals.models import ApprovalRequest, LedgerEntry
from core.interfaces import IBaseRepository


class IApprovalRequestRepository(IBaseRepository[ApprovalRequest]):
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[ApprovalRequest]:
        raise NotImplementedError

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> ApprovalRequest | None:
        raise NotImplementedError


class ILedgerEntryRepository(IBaseRepository[LedgerEntry]):
    def list_entries(self) -> QuerySet[LedgerEntry]:
        raise NotImplementedError
