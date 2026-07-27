"""Assignment-domain repository ports.

Read scoping is role-based (delegated to ``apps.assignments.selectors``): a
director/HoD/superuser sees all; a teacher sees their taught cohorts' assignments +
submissions (incl. drafts); a student sees only PUBLISHED assignments in their own
cohorts and their own submissions. Out-of-scope rows are filtered OUT (so they 404,
never a 403 that leaks existence).
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.assignments.models import Assignment, Submission
from core.interfaces import IBaseRepository


class IAssignmentRepository(IBaseRepository[Assignment]):
    def scoped(self, *, user, roles: set[str]) -> QuerySet[Assignment]:
        raise NotImplementedError

    def get_scoped(self, *, user, roles: set[str], pk: int) -> Assignment | None:
        raise NotImplementedError


class ISubmissionRepository(IBaseRepository[Submission]):
    def scoped(self, *, user, roles: set[str]) -> QuerySet[Submission]:
        raise NotImplementedError

    def get_scoped(self, *, user, roles: set[str], pk: int) -> Submission | None:
        raise NotImplementedError
