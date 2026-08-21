"""Cohort repository ports."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher
from apps.teachers.models import TeacherType
from core.interfaces import IBaseRepository


class ICohortRepository(IBaseRepository[Cohort]):
    def has_memberships(self, cohort: Cohort) -> bool:
        """True if the cohort has any membership rows (history must not be deleted)."""
        raise NotImplementedError

    def active_members(self, cohort: Cohort) -> QuerySet[CohortMembership]:
        """Active (not-yet-end-dated) memberships of the cohort, teacher-friendly order."""
        raise NotImplementedError

    def teacher_types(self) -> QuerySet[TeacherType]:
        raise NotImplementedError

    def get_teacher_type(self, teacher_type_id: int) -> TeacherType | None:
        raise NotImplementedError

    def teacher_assignments(self, cohort: Cohort) -> QuerySet[CohortTeacher]:
        raise NotImplementedError

    def get_teacher_assignment(self, cohort: Cohort, assignment_id: int) -> CohortTeacher | None:
        raise NotImplementedError
