"""Sales-domain repository port.

Sales rows are branch-scoped to the seller's own till: a director/superuser sees every
sale; any other role sees only sales whose branch is one of their role-membership
branches. Out-of-scope rows are filtered OUT (so a detail/refund 404s, never a 403 that
leaks existence — matching the DRF ViewSet's get_object()).
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.sales.models import Sale
from apps.students.models import StudentProfile
from core.interfaces import IBaseRepository


class ISaleRepository(IBaseRepository[Sale]):
    def scoped(self, *, is_unscoped: bool, branch_ids: set[int]) -> QuerySet[Sale]:
        raise NotImplementedError

    def get_scoped(self, *, is_unscoped: bool, branch_ids: set[int], pk: int) -> Sale | None:
        raise NotImplementedError

    def get_student(self, *, student_id: int) -> StudentProfile | None:
        raise NotImplementedError
