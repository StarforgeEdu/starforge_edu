"""ORM-backed task repository (role-scoped reads)."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.tasks.interfaces.repositories import ITaskRepository
from apps.tasks.models import Task
from core.repositories import BaseRepository


class TaskRepository(BaseRepository[Task], ITaskRepository):
    model = Task

    def get_queryset(self) -> QuerySet[Task]:
        return Task.objects.select_related("assignee", "department", "branch", "created_by")

    def scoped(
        self, *, user, is_unscoped: bool, has_write: bool, branch_ids: set[int], dept_ids: set[int]
    ) -> QuerySet[Task]:
        qs = self.get_queryset()
        if is_unscoped:
            return qs
        # Everyone sees tasks assigned to them or that they created; a department member
        # sees their department's tasks; a tasks:write holder sees their branch(es)' tasks.
        scope = Q(assignee=user) | Q(created_by=user)
        if dept_ids:
            scope |= Q(department_id__in=dept_ids)
        if has_write and branch_ids:
            scope |= Q(branch_id__in=branch_ids)
        return qs.filter(scope)

    def get_scoped(
        self, *, user, is_unscoped: bool, has_write: bool, branch_ids: set[int], dept_ids: set[int], pk: int
    ) -> Task | None:
        return (
            self.scoped(
                user=user,
                is_unscoped=is_unscoped,
                has_write=has_write,
                branch_ids=branch_ids,
                dept_ids=dept_ids,
            )
            .filter(pk=pk)
            .first()
        )

    def assigned_to(self, user) -> QuerySet[Task]:
        return self.get_queryset().filter(assignee=user)
