"""Org-domain repository ports."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.org.models import Branch, BranchTransfer, Department, Room
from core.interfaces import IBaseRepository


class IBranchRepository(IBaseRepository[Branch]):
    def active(self) -> QuerySet[Branch]:
        """Non-archived branches with departments + working_hours prefetched."""
        raise NotImplementedError


class IDepartmentRepository(IBaseRepository[Department]):
    ...


class IRoomRepository(IBaseRepository[Room]):
    ...


class IBranchTransferRepository(IBaseRepository[BranchTransfer]):
    ...
