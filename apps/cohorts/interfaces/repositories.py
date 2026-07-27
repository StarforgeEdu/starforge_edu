"""Cohort repository ports."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.cohorts.models import Cohort, CohortMembership
from core.interfaces import IBaseRepository


class ICohortRepository(IBaseRepository[Cohort]):
    def has_memberships(self, cohort: Cohort) -> bool:
        """True if the cohort has any membership rows (history must not be deleted)."""
        raise NotImplementedError

    def active_members(self, cohort: Cohort) -> QuerySet[CohortMembership]:
        """Active (not-yet-end-dated) memberships of the cohort, teacher-friendly order."""
        raise NotImplementedError
