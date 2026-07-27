"""ORM-backed branch-transfer repository (read-only audit list)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.org.interfaces.repositories import IBranchTransferRepository
from apps.org.models import BranchTransfer
from core.repositories import BaseRepository


class BranchTransferRepository(BaseRepository[BranchTransfer], IBranchTransferRepository):
    model = BranchTransfer

    def get_queryset(self) -> QuerySet[BranchTransfer]:
        return BranchTransfer.objects.select_related("from_branch", "to_branch", "user", "actor")
