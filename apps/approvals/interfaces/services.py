"""Approvals-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.approvals.models import ApprovalRequest, LedgerEntry


class IApprovalService(ABC):
    @abstractmethod
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[ApprovalRequest]: ...

    @abstractmethod
    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> ApprovalRequest | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any], requested_by) -> ApprovalRequest: ...

    @abstractmethod
    def approve(self, *, request_id: int, actor, note: str) -> ApprovalRequest: ...

    @abstractmethod
    def reject(self, *, request_id: int, actor, note: str) -> ApprovalRequest: ...

    @abstractmethod
    def cancel(self, *, request_id: int, actor) -> ApprovalRequest: ...

    @abstractmethod
    def disburse(
        self,
        *,
        request_id: int,
        payment_method_id: int,
        actor,
        direction: str,
        entry_type: str,
        party_label: str,
    ) -> ApprovalRequest: ...


class ILedgerService(ABC):
    @abstractmethod
    def list_entries(self) -> QuerySet[LedgerEntry]: ...
