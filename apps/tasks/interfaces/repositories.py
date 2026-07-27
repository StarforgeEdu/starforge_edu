"""Task-domain repository ports.

Task reads are role-scoped: a director/superuser sees the whole centre; everyone else
sees tasks assigned to them, ones they created, tasks in their department(s), and — if
they hold tasks:write — their branch(es)' tasks. RoleGrade is an unscoped centre-wide
hierarchy table.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.tasks.models import RoleGrade, Task
from core.interfaces import IBaseRepository


class ITaskRepository(IBaseRepository[Task]):
    def scoped(
        self, *, user, is_unscoped: bool, has_write: bool, branch_ids: set[int], dept_ids: set[int]
    ) -> QuerySet[Task]:
        raise NotImplementedError

    def get_scoped(
        self, *, user, is_unscoped: bool, has_write: bool, branch_ids: set[int], dept_ids: set[int], pk: int
    ) -> Task | None:
        raise NotImplementedError

    def assigned_to(self, user) -> QuerySet[Task]:
        """The caller's own assigned tasks (the `mine` list)."""
        raise NotImplementedError


class IRoleGradeRepository(IBaseRepository[RoleGrade]): ...
