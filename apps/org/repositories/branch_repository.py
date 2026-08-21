"""ORM-backed branch repository.

Departments are exposed only by their branch-scoped endpoint; the tenant-wide
branch directory deliberately does not prefetch or embed their sensitive budget
and head metadata.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.org.interfaces.repositories import IBranchRepository
from apps.org.models import Branch
from core.repositories import BaseRepository


class BranchRepository(BaseRepository[Branch], IBranchRepository):
    model = Branch

    def get_queryset(self) -> QuerySet[Branch]:
        return Branch.objects.prefetch_related("working_hours")

    def active(self) -> QuerySet[Branch]:
        # Archived branches drop out of the default surface (D1-LF-7).
        return self.get_queryset().filter(archived_at__isnull=True)
