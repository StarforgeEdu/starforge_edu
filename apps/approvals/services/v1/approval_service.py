"""Approvals application services (delegation to the preserved A-1 engine domain
functions). Ledger reads are a thin pass-through over the repository."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.approvals import services as domain
from apps.approvals.interfaces.repositories import (
    IApprovalRequestRepository,
    ILedgerEntryRepository,
)
from apps.approvals.interfaces.services import IApprovalService, ILedgerService
from apps.approvals.models import ApprovalRequest, LedgerEntry
from core.exceptions import NotFoundException, ValidationException


class ApprovalService(IApprovalService):
    def __init__(self, repository: IApprovalRequestRepository) -> None:
        self.repository = repository

    def scoped(
        self,
        *,
        user: Any,
        roles: set[str] | None,
        permission: str | None = "approvals:read",
        include_requested_by: bool = True,
    ) -> QuerySet[ApprovalRequest]:
        return self.repository.scoped(
            user=user,
            roles=roles,
            permission=permission,
            include_requested_by=include_requested_by,
        )

    def get_scoped(
        self,
        *,
        pk: int,
        user: Any,
        roles: set[str] | None,
        permission: str | None = "approvals:read",
        include_requested_by: bool = True,
    ) -> ApprovalRequest | None:
        return self.repository.get_scoped(
            pk=pk,
            user=user,
            roles=roles,
            permission=permission,
            include_requested_by=include_requested_by,
        )

    def create(
        self,
        *,
        data: dict[str, Any],
        requested_by,
        allowed_branch_ids: set[int] | None,
    ) -> ApprovalRequest:
        branch = None
        branch_id = data.get("branch")
        if branch_id is not None:
            if allowed_branch_ids is not None and branch_id not in allowed_branch_ids:
                raise NotFoundException(code="not_found")
            from apps.org.models import Branch

            branch = Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
            if branch is None:
                raise ValidationException(
                    _("Invalid input."),
                    code="validation_error",
                    fields={"branch": ["Branch does not exist."]},
                )
        return domain.create_request(
            requested_by=requested_by,
            kind=data["kind"],
            title=data["title"],
            description=data.get("description", ""),
            amount_uzs=data.get("amount_uzs"),
            branch=branch,
            payload=data.get("payload", {}),
            allowed_branch_ids=allowed_branch_ids,
        )

    def approve(self, *, request_id: int, actor, note: str) -> ApprovalRequest:
        return domain.approve(request_id=request_id, actor=actor, note=note)

    def reject(self, *, request_id: int, actor, note: str) -> ApprovalRequest:
        return domain.reject(request_id=request_id, actor=actor, note=note)

    def cancel(self, *, request_id: int, actor) -> ApprovalRequest:
        return domain.cancel(request_id=request_id, actor=actor)

    def disburse(
        self,
        *,
        request_id: int,
        payment_method_id: int,
        actor,
        direction: str,
        entry_type: str,
        party_label: str,
    ) -> ApprovalRequest:
        return domain.disburse(
            request_id=request_id,
            payment_method_id=payment_method_id,
            actor=actor,
            direction=direction,
            entry_type=entry_type,
            party_label=party_label,
        )


class LedgerService(ILedgerService):
    def __init__(self, repository: ILedgerEntryRepository) -> None:
        self.repository = repository

    def list_entries(self) -> QuerySet[LedgerEntry]:
        return self.repository.list_entries()
