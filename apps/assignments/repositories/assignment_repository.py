"""ORM-backed assignment + submission repositories (role-scoped reads).

Both delegate the (nuanced, role-based) scoping to ``apps.assignments.selectors`` — the
single read-scoping module (also exercised directly by the tests), so the repository is
a thin, layered adapter over it.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.assignments.interfaces.repositories import IAssignmentRepository, ISubmissionRepository
from apps.assignments.models import Assignment, Submission
from apps.assignments.selectors import scoped_assignments, scoped_submissions
from core.repositories import BaseRepository


class AssignmentRepository(BaseRepository[Assignment], IAssignmentRepository):
    model = Assignment

    def scoped(self, *, user, roles: set[str]) -> QuerySet[Assignment]:
        return scoped_assignments(user=user, roles=roles)

    def get_scoped(self, *, user, roles: set[str], pk: int) -> Assignment | None:
        return self.scoped(user=user, roles=roles).filter(pk=pk).first()


class SubmissionRepository(BaseRepository[Submission], ISubmissionRepository):
    model = Submission

    def scoped(self, *, user, roles: set[str]) -> QuerySet[Submission]:
        return scoped_submissions(user=user, roles=roles)

    def get_scoped(self, *, user, roles: set[str], pk: int) -> Submission | None:
        return self.scoped(user=user, roles=roles).filter(pk=pk).first()
