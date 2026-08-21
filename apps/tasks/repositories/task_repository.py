"""ORM-backed task repository (role-scoped reads)."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.tasks.interfaces.repositories import ITaskRepository
from apps.tasks.models import Task
from core.repositories import BaseRepository


class TaskRepository(BaseRepository[Task], ITaskRepository):
    model = Task

    def get_queryset(self) -> QuerySet[Task]:
        return Task.objects.select_related(
            "assignee",
            "assignee__staff_profile",
            "assignee__teacher_profile",
            "department",
            "branch",
            "created_by",
            "created_by__staff_profile",
            "created_by__teacher_profile",
        )

    def scoped(
        self,
        *,
        is_unscoped: bool,
        include_assignee: bool,
        principal_kind: str,
        principal_id: int,
        branch_ids: set[int],
        dept_ids: set[int],
    ) -> QuerySet[Task]:
        qs = self.get_queryset()
        if is_unscoped:
            return qs
        scope = Q(pk__in=[])
        if include_assignee:
            scope |= Q(
                assignee_principal_kind=principal_kind,
                assignee_principal_id=principal_id,
                assignee_attribution_status="captured",
            )
        if branch_ids:
            scope |= Q(branch_id__in=branch_ids)
        if dept_ids:
            scope |= Q(department_id__in=dept_ids)
        return qs.filter(scope)

    def get_scoped(
        self,
        *,
        is_unscoped: bool,
        include_assignee: bool,
        principal_kind: str,
        principal_id: int,
        branch_ids: set[int],
        dept_ids: set[int],
        pk: int,
    ) -> Task | None:
        return (
            self.scoped(
                is_unscoped=is_unscoped,
                include_assignee=include_assignee,
                principal_kind=principal_kind,
                principal_id=principal_id,
                branch_ids=branch_ids,
                dept_ids=dept_ids,
            )
            .filter(pk=pk)
            .first()
        )

    def assigned_to(self, *, principal_kind: str, principal_id: int) -> QuerySet[Task]:
        return self.get_queryset().filter(
            assignee_principal_kind=principal_kind,
            assignee_principal_id=principal_id,
            assignee_attribution_status="captured",
        )
