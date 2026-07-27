"""Task-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.tasks.dto.task_dto import AssignTaskDTO, CreateTaskDTO, RoleGradeDTO
from apps.tasks.models import RoleGrade, Task


class ITaskService(ABC):
    @abstractmethod
    def scoped_list(
        self, *, user, is_unscoped: bool, has_write: bool, branch_ids: set[int], dept_ids: set[int]
    ) -> QuerySet[Task]: ...

    @abstractmethod
    def get_visible(
        self, *, user, is_unscoped: bool, has_write: bool, branch_ids: set[int], dept_ids: set[int], pk: int
    ) -> Task | None: ...

    @abstractmethod
    def mine(self, user) -> QuerySet[Task]: ...

    @abstractmethod
    def create(
        self,
        data: CreateTaskDTO,
        *,
        creator,
        creator_roles: set[str],
        is_superuser: bool,
        is_unscoped: bool,
        branch_ids: set[int],
    ) -> Task: ...

    @abstractmethod
    def assign(
        self,
        task: Task,
        data: AssignTaskDTO,
        *,
        actor,
        actor_roles: set[str],
        is_unscoped: bool,
        branch_ids: set[int],
    ) -> Task: ...

    @abstractmethod
    def transition(self, task: Task, *, to_status: str, actor) -> Task: ...

    @abstractmethod
    def auto_assign(
        self,
        *,
        task_ids: list[int],
        department_id: int,
        actor,
        actor_roles: set[str],
        mode: str,
        is_unscoped: bool,
        branch_ids: set[int],
    ) -> dict[str, Any]: ...


class IRoleGradeService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[RoleGrade]: ...

    @abstractmethod
    def get(self, pk: int) -> RoleGrade | None: ...

    @abstractmethod
    def create(self, data: RoleGradeDTO) -> RoleGrade: ...

    @abstractmethod
    def update(self, grade: RoleGrade, changes: dict[str, Any]) -> RoleGrade: ...

    @abstractmethod
    def delete(self, grade: RoleGrade) -> None: ...
