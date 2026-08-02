"""BranchTransferService — scoped audit reads + transactional student moves."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.org.interfaces.repositories import IBranchTransferRepository
from apps.org.interfaces.services import IBranchTransferService
from apps.org.models import BranchTransfer


class BranchTransferService(IBranchTransferService):
    def __init__(self, transfers: IBranchTransferRepository) -> None:
        self._transfers = transfers

    def list(self) -> QuerySet[BranchTransfer]:
        return self._transfers.get_queryset()

    def get(self, transfer_id: int) -> BranchTransfer | None:
        return self._transfers.get_by_id(transfer_id)

    def transfer_student(
        self,
        *,
        student_id: int,
        to_branch_id: int,
        reason: str,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int | None,
        allowed_branch_ids: set[int] | None,
    ) -> BranchTransfer:
        from apps.org.services import transfer_student

        return transfer_student(
            student_id=student_id,
            to_branch_id=to_branch_id,
            reason=reason,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
            allowed_branch_ids=allowed_branch_ids,
        )
