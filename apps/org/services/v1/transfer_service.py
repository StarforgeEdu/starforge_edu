"""BranchTransferService — read-only audit list."""

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
