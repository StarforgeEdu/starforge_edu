"""TaskService — the layered facade over the task domain functions.

Read scoping is delegated to the repository; create/assign/transition/auto-assign route
through the transactional domain functions (which hold the hierarchy gate + lifecycle).
FK inputs (assignee/department/branch) are resolved here → clean 400s, and the
branch-containment scope check (a non-director may only place work in their own branch /
a department of their branch) lives here too since it needs the resolved objects.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.tasks.dto.task_dto import AssignTaskDTO, CreateTaskDTO
from apps.tasks.interfaces.repositories import ITaskRepository
from apps.tasks.interfaces.services import ITaskService
from apps.tasks.models import Task
from core.exceptions import PermissionException, ValidationException
from core.permissions import Role

# Who may be tasked: staff, i.e. everyone except students and parents.
_TASKABLE_ROLES = tuple(r for r in Role.ALL if r not in (Role.STUDENT, Role.PARENT))


def _assert_scope(is_unscoped: bool, branch, department, branch_ids: set[int]) -> None:
    """A non-director may only place a task in their own branch / a department of their
    branch — otherwise they could plant work in another branch (an intra-tenant leak)."""
    if is_unscoped:
        return
    if branch is not None and branch.id not in branch_ids:
        raise PermissionException(_("You can only use your own branch."), code="cross_branch")
    if department is not None and department.branch_id not in branch_ids:
        raise PermissionException(
            _("You can only use a department in your own branch."), code="cross_branch_dept"
        )


class TaskService(ITaskService):
    def __init__(self, tasks: ITaskRepository) -> None:
        self._tasks = tasks

    def scoped_list(
        self, *, user, is_unscoped: bool, has_write: bool, branch_ids: set[int], dept_ids: set[int]
    ) -> QuerySet[Task]:
        return self._tasks.scoped(
            user=user, is_unscoped=is_unscoped, has_write=has_write, branch_ids=branch_ids, dept_ids=dept_ids
        )

    def get_visible(
        self, *, user, is_unscoped: bool, has_write: bool, branch_ids: set[int], dept_ids: set[int], pk: int
    ) -> Task | None:
        return self._tasks.get_scoped(
            user=user,
            is_unscoped=is_unscoped,
            has_write=has_write,
            branch_ids=branch_ids,
            dept_ids=dept_ids,
            pk=pk,
        )

    def mine(self, user) -> QuerySet[Task]:
        return self._tasks.assigned_to(user)

    def create(
        self,
        data: CreateTaskDTO,
        *,
        creator,
        creator_roles: set[str],
        is_superuser: bool,
        is_unscoped: bool,
        branch_ids: set[int],
    ) -> Task:
        from apps.tasks.services import create_task

        assignee = self._resolve_assignee(data.assignee_id)
        department = self._resolve_department(data.department_id)
        branch = self._resolve_branch(data.branch_id)
        # Default a single-branch (non-superuser) creator to their own branch — matches
        # the old view's create() convenience.
        if branch is None and not is_superuser and len(branch_ids) == 1:
            branch = self._branch_by_id(next(iter(branch_ids)))
        _assert_scope(is_unscoped, branch, department, branch_ids)
        return create_task(
            title=data.title,
            created_by=creator,
            created_by_roles=creator_roles,
            assignee=assignee,
            department=department,
            branch=branch,
            description=data.description,
            priority=data.priority,
            due_at=data.due_at,
        )

    def assign(
        self,
        task: Task,
        data: AssignTaskDTO,
        *,
        actor,
        actor_roles: set[str],
        is_unscoped: bool,
        branch_ids: set[int],
    ) -> Task:
        from apps.tasks.services import assign_task

        if not data.assignee_provided and not data.department_provided:
            raise ValidationException(_("Provide an assignee and/or a department."), code="validation_error")
        kwargs: dict[str, Any] = {}
        if data.assignee_provided:
            kwargs["assignee"] = self._resolve_assignee(data.assignee_id)
        if data.department_provided:
            department = self._resolve_department(data.department_id)
            _assert_scope(is_unscoped, None, department, branch_ids)  # own-branch dept only
            kwargs["department"] = department
        return assign_task(task=task, actor=actor, actor_roles=actor_roles, **kwargs)

    def transition(self, task: Task, *, to_status: str, actor) -> Task:
        from apps.tasks.services import transition_task

        if to_status not in Task.Status.values:  # mirrors the old ChoiceField
            raise ValidationException(
                _("Invalid status."),
                code="validation_error",
                fields={"status": [f"Must be one of {', '.join(Task.Status.values)}."]},
            )
        return transition_task(task=task, to_status=to_status, actor=actor)

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
    ) -> dict[str, Any]:
        from apps.tasks.services import auto_split_tasks

        department = self._resolve_department(department_id, required=True)
        _assert_scope(is_unscoped, None, department, branch_ids)  # only your own branch's dept
        return auto_split_tasks(
            task_ids=task_ids, department=department, actor=actor, actor_roles=actor_roles, mode=mode
        )

    # --- FK resolution (bad/missing id -> 400 field error, never a 500) --------
    @staticmethod
    def _resolve_assignee(assignee_id: int | None):
        if assignee_id is None:
            return None
        from apps.users.models import User

        # Only staff (an active staff RoleMembership in this centre) can be tasked — never
        # a student/parent, nor a membership-less user (mirrors the old serializer queryset).
        assignee = (
            User.objects.filter(
                pk=assignee_id,
                is_active=True,
                role_memberships__revoked_at__isnull=True,
                role_memberships__role__in=_TASKABLE_ROLES,
            )
            .distinct()
            .first()
        )
        if assignee is None:
            raise ValidationException(
                _("Invalid assignee."),
                code="validation_error",
                fields={"assignee": ["Not a valid staff member in this centre."]},
            )
        return assignee

    @staticmethod
    def _resolve_department(department_id: int | None, *, required: bool = False):
        if department_id is None:
            if required:
                raise ValidationException(
                    _("A department is required."),
                    code="validation_error",
                    fields={"department": ["This field is required."]},
                )
            return None
        from apps.org.models import Department

        department = Department.objects.filter(pk=department_id).first()
        if department is None:
            raise ValidationException(
                _("Invalid department."),
                code="validation_error",
                fields={"department": ["Not found."]},
            )
        return department

    @staticmethod
    def _resolve_branch(branch_id: int | None):
        if branch_id is None:
            return None
        from apps.org.models import Branch

        branch = Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
        if branch is None:  # mirrors the old serializer's non-archived branch queryset
            raise ValidationException(
                _("Invalid branch."), code="validation_error", fields={"branch": ["Not found."]}
            )
        return branch

    @staticmethod
    def _branch_by_id(branch_id: int):
        from apps.org.models import Branch

        return Branch.objects.filter(pk=branch_id).first()
