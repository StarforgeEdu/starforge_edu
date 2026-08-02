"""Task-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.tasks.dto.task_dto import AssignTaskDTO, CreateTaskDTO, RoleGradeDTO
from apps.tasks.models import RoleGrade, Task
from core.permissions import MembershipGrantScope
from core.role_principals import RolePrincipal


class ITaskService(ABC):
    @abstractmethod
    def scoped_list(
        self,
        *,
        is_unscoped: bool,
        include_assignee: bool,
        principal_kind: str,
        principal_id: int,
        branch_ids: set[int],
        dept_ids: set[int],
    ) -> QuerySet[Task]: ...

    @abstractmethod
    def get_visible(
        self,
        *,
        is_unscoped: bool,
        include_assignee: bool,
        principal_kind: str,
        principal_id: int,
        branch_ids: set[int],
        dept_ids: set[int],
        pk: int,
    ) -> Task | None: ...

    @abstractmethod
    def mine(self, *, principal_kind: str, principal_id: int) -> QuerySet[Task]: ...

    @abstractmethod
    def create(
        self,
        data: CreateTaskDTO,
        *,
        creator,
        creator_principal: RolePrincipal,
        is_superuser: bool,
        is_unscoped: bool,
        write_grants: tuple[MembershipGrantScope, ...],
        assign_any_grants: tuple[MembershipGrantScope, ...],
    ) -> Task: ...

    @abstractmethod
    def assign(
        self,
        task: Task,
        data: AssignTaskDTO,
        *,
        actor,
        is_unscoped: bool,
        write_grants: tuple[MembershipGrantScope, ...],
        assign_any_grants: tuple[MembershipGrantScope, ...],
    ) -> Task: ...

    @abstractmethod
    def transition(
        self,
        task: Task,
        *,
        to_status: str,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
        is_superuser: bool,
        transition_grants: tuple[MembershipGrantScope, ...],
        assign_any_grants: tuple[MembershipGrantScope, ...],
    ) -> Task: ...

    @abstractmethod
    def auto_assign(
        self,
        *,
        task_ids: list[int],
        department_id: int,
        actor,
        mode: str,
        is_unscoped: bool,
        write_grants: tuple[MembershipGrantScope, ...],
        assign_any_grants: tuple[MembershipGrantScope, ...],
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
