"""ORM-backed branch repository. The list/detail presenters render the branch's
departments + working_hours, so both are prefetched to keep the list query flat."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.org.interfaces.repositories import IBranchRepository
from apps.org.models import Branch
from core.repositories import BaseRepository


class BranchRepository(BaseRepository[Branch], IBranchRepository):
    model = Branch

    def get_queryset(self) -> QuerySet[Branch]:
        return Branch.objects.prefetch_related("departments__head__teacher_profile", "working_hours")

    def active(self) -> QuerySet[Branch]:
        # Archived branches drop out of the default surface (D1-LF-7).
        return self.get_queryset().filter(archived_at__isnull=True)
