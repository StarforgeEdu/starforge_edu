"""TaskService — the layered facade over the task domain functions.

Read scoping is delegated to the repository; create/assign/transition/auto-assign route
through the transactional domain functions (which hold the hierarchy gate + lifecycle).
FK inputs (assignee/department/branch) are resolved here → clean 400s, and the
branch-containment scope check (a non-director may only place work in their own branch /
a department of their branch) lives here too since it needs the resolved objects.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.tasks.dto.task_dto import AssignTaskDTO, CreateTaskDTO, TaskAssignee
from apps.tasks.interfaces.repositories import ITaskRepository
from apps.tasks.interfaces.services import ITaskService
from apps.tasks.models import Task
from core.exceptions import PermissionException, ValidationException
from core.permissions import MembershipGrantScope
from core.role_principals import (
    STAFF_PRINCIPAL_KINDS,
    RolePrincipal,
    resolve_unambiguous_user_principal,
)


def _grant_covers(grant: MembershipGrantScope, *, branch_id: int | None, department_id: int | None) -> bool:
    if grant.is_organization_wide:
        return True
    if branch_id is None or grant.branch_id != branch_id:
        return False
    return grant.department_id is None or grant.department_id == department_id


def _assert_scope(
    is_unscoped: bool,
    branch,
    department,
    grants: tuple[MembershipGrantScope, ...],
) -> None:
    """A non-director may only place a task in their own branch / a department of their
    branch — otherwise they could plant work in another branch (an intra-tenant leak)."""
    if is_unscoped:
        return
    branch_id = branch.pk if branch is not None else None
    department_id = department.pk if department is not None else None
    if branch_id is None or not any(
        _grant_covers(grant, branch_id=branch_id, department_id=department_id) for grant in grants
    ):
        raise PermissionException(_("The task is outside your task scope."), code="out_of_scope")


def _roles_for_boundary(
    grants: tuple[MembershipGrantScope, ...], *, branch_id: int | None, department_id: int | None
) -> set[str]:
    return {
        grant.role
        for grant in grants
        if _grant_covers(grant, branch_id=branch_id, department_id=department_id)
    }


def _has_boundary_grant(
    grants: tuple[MembershipGrantScope, ...], *, branch_id: int | None, department_id: int | None
) -> bool:
    return any(_grant_covers(grant, branch_id=branch_id, department_id=department_id) for grant in grants)


class TaskService(ITaskService):
    def __init__(self, tasks: ITaskRepository) -> None:
        self._tasks = tasks

    def scoped_list(
        self,
        *,
        is_unscoped: bool,
        include_assignee: bool,
        principal_kind: str,
        principal_id: int,
        branch_ids: set[int],
        dept_ids: set[int],
    ) -> QuerySet[Task]:
        return self._tasks.scoped(
            is_unscoped=is_unscoped,
            include_assignee=include_assignee,
            principal_kind=principal_kind,
            principal_id=principal_id,
            branch_ids=branch_ids,
            dept_ids=dept_ids,
        )

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
    ) -> Task | None:
        return self._tasks.get_scoped(
            is_unscoped=is_unscoped,
            include_assignee=include_assignee,
            principal_kind=principal_kind,
            principal_id=principal_id,
            branch_ids=branch_ids,
            dept_ids=dept_ids,
            pk=pk,
        )

    def mine(self, *, principal_kind: str, principal_id: int) -> QuerySet[Task]:
        return self._tasks.assigned_to(principal_kind=principal_kind, principal_id=principal_id)

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
    ) -> Task:
        from apps.tasks.services import create_task

        department = self._resolve_department(
            data.department_id,
            allowed_grants=None if is_unscoped else write_grants,
        )
        branch = None
        if department is not None:
            if data.branch_id is not None and data.branch_id != department.branch_id:
                raise ValidationException(
                    _("Branch and department must match."),
                    code="validation_error",
                    fields={"department": [_("Choose a department in the selected branch.")]},
                )
            branch = department.branch
        else:
            branch = self._resolve_branch(
                data.branch_id,
                allowed_branch_ids=(
                    None
                    if is_unscoped
                    else {grant.branch_id for grant in write_grants if grant.department_id is None}
                ),
            )
        if branch is None and department is None and not is_unscoped:
            # A scoped staff member should be able to create a personal task
            # without knowing internal branch/department identifiers. Infer only
            # when their effective write grants collapse to one exact boundary;
            # ambiguous memberships remain fail-closed and require an explicit
            # selection. Department-scoped grants must stay department-scoped —
            # treating one as a branch-wide grant would expose another team's
            # backlog.
            boundaries = {
                (grant.branch_id, grant.department_id)
                for grant in write_grants
                if not grant.is_organization_wide
            }
            if len(boundaries) == 1:
                inferred_branch_id, inferred_department_id = next(iter(boundaries))
                if inferred_department_id is not None:
                    department = self._resolve_department(
                        inferred_department_id,
                        allowed_grants=write_grants,
                    )
                    branch = department.branch
                else:
                    branch = self._resolve_branch(
                        inferred_branch_id,
                        allowed_branch_ids={inferred_branch_id},
                    )
        _assert_scope(is_unscoped, branch, department, write_grants)
        assignee = self._resolve_assignee(
            data.assignee_id,
            selected_kind=data.assignee_principal_kind,
            selected_id=data.assignee_principal_id,
            branch_id=branch.pk if branch is not None else None,
            department_id=department.pk if department is not None else None,
        )
        actor_roles = _roles_for_boundary(
            write_grants,
            branch_id=branch.pk if branch is not None else None,
            department_id=department.pk if department is not None else None,
        )
        return create_task(
            title=data.title,
            created_by=creator,
            created_by_principal=creator_principal,
            created_by_roles=actor_roles,
            assignee=assignee.user if assignee is not None else None,
            assignee_principal_kind=assignee.principal_kind if assignee is not None else "",
            assignee_principal_id=assignee.principal_id if assignee is not None else None,
            assignee_roles=set(assignee.roles) if assignee is not None else None,
            can_assign_any=_has_boundary_grant(
                assign_any_grants,
                branch_id=branch.pk if branch is not None else None,
                department_id=department.pk if department is not None else None,
            ),
            department=department,
            branch=branch,
            description=data.description,
            priority=data.priority,
            due_at=data.due_at,
        )

    @transaction.atomic
    def assign(
        self,
        task: Task,
        data: AssignTaskDTO,
        *,
        actor,
        is_unscoped: bool,
        write_grants: tuple[MembershipGrantScope, ...],
        assign_any_grants: tuple[MembershipGrantScope, ...],
    ) -> Task:
        from apps.tasks.services import assign_task

        # PostgreSQL cannot lock the nullable side of the OUTER JOIN generated by
        # select_related() for these optional relationships.  Lock only the task
        # row; resolving either relationship afterwards is still protected by the
        # task lock and avoids a production 500 for unscoped/unassigned tasks.
        task = Task.objects.select_for_update().get(pk=task.pk)
        if not data.assignee_provided and not data.department_provided:
            raise ValidationException(_("Provide an assignee and/or a department."), code="validation_error")
        final_department = task.department
        if data.department_provided:
            final_department = self._resolve_department(
                data.department_id,
                allowed_grants=None if is_unscoped else write_grants,
            )
        final_branch = task.branch
        if final_department is not None:
            if final_branch is not None and final_branch.pk != final_department.branch_id:
                raise ValidationException(
                    _("The department is outside the task's branch."),
                    code="validation_error",
                    fields={"department": [_("Choose a department in the task's branch.")]},
                )
            final_branch = final_department.branch
        _assert_scope(is_unscoped, final_branch, final_department, write_grants)

        resolved_assignee: TaskAssignee | None = None
        if data.assignee_provided and data.assignee_id is not None:
            resolved_assignee = self._resolve_assignee(
                data.assignee_id,
                selected_kind=data.assignee_principal_kind,
                selected_id=data.assignee_principal_id,
                branch_id=final_branch.pk if final_branch is not None else None,
                department_id=final_department.pk if final_department is not None else None,
            )
        elif not data.assignee_provided and task.assignee_id is not None:
            resolved_assignee = self._resolve_assignee(
                task.assignee_id,
                branch_id=final_branch.pk if final_branch is not None else None,
                department_id=final_department.pk if final_department is not None else None,
                expected_kind=task.assignee_principal_kind,
                expected_id=task.assignee_principal_id,
            )

        kwargs: dict[str, Any] = {}
        if data.assignee_provided:
            kwargs.update(
                {
                    "assignee": resolved_assignee.user if resolved_assignee is not None else None,
                    "assignee_principal_kind": (
                        resolved_assignee.principal_kind if resolved_assignee is not None else ""
                    ),
                    "assignee_principal_id": (
                        resolved_assignee.principal_id if resolved_assignee is not None else None
                    ),
                    "assignee_roles": (
                        set(resolved_assignee.roles) if resolved_assignee is not None else None
                    ),
                }
            )
        if data.department_provided:
            kwargs["department"] = final_department
            kwargs["branch"] = final_branch
        actor_roles = _roles_for_boundary(
            write_grants,
            branch_id=final_branch.pk if final_branch is not None else None,
            department_id=final_department.pk if final_department is not None else None,
        )
        return assign_task(
            task=task,
            actor=actor,
            actor_roles=actor_roles,
            can_assign_any=_has_boundary_grant(
                assign_any_grants,
                branch_id=final_branch.pk if final_branch is not None else None,
                department_id=final_department.pk if final_department is not None else None,
            ),
            **kwargs,
        )

    @transaction.atomic
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
    ) -> Task:
        from apps.tasks.services import transition_task

        if to_status not in Task.Status.values:  # mirrors the old ChoiceField
            raise ValidationException(
                _("Invalid status."),
                code="validation_error",
                fields={"status": [f"Must be one of {', '.join(Task.Status.values)}."]},
            )
        task = Task.objects.select_for_update().get(pk=task.pk)
        can_transition_any = (
            is_superuser
            or _has_boundary_grant(
                transition_grants,
                branch_id=task.branch_id,
                department_id=task.department_id,
            )
            or _has_boundary_grant(
                assign_any_grants,
                branch_id=task.branch_id,
                department_id=task.department_id,
            )
        )
        return transition_task(
            task=task,
            to_status=to_status,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
            can_transition_any=can_transition_any,
        )

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
    ) -> dict[str, Any]:
        from apps.tasks.services import auto_split_tasks

        department = self._resolve_department(
            department_id,
            required=True,
            allowed_grants=None if is_unscoped else write_grants,
        )
        _assert_scope(is_unscoped, department.branch, department, write_grants)
        return auto_split_tasks(
            task_ids=task_ids,
            department=department,
            actor=actor,
            actor_roles=_roles_for_boundary(
                write_grants,
                branch_id=department.branch_id,
                department_id=department.pk,
            ),
            can_assign_any=_has_boundary_grant(
                assign_any_grants,
                branch_id=department.branch_id,
                department_id=department.pk,
            ),
            mode=mode,
        )

    # --- FK resolution (bad/missing id -> 400 field error, never a 500) --------
    @staticmethod
    def _resolve_assignee(
        assignee_id: int | None,
        *,
        branch_id: int | None,
        department_id: int | None,
        expected_kind: str | None = None,
        expected_id: int | None = None,
        selected_kind: str | None = None,
        selected_id: int | None = None,
    ) -> TaskAssignee | None:
        explicit_target = selected_kind is not None or selected_id is not None
        if assignee_id is None and not explicit_target:
            return None
        invalid = ValidationException(
            _("Invalid assignee."),
            code="validation_error",
            fields={"assignee": [_("Choose active task staff in the task's scope.")]},
        )
        if assignee_id is not None and explicit_target:
            raise invalid
        principal = None
        if explicit_target:
            if (
                selected_kind not in STAFF_PRINCIPAL_KINDS
                or isinstance(selected_id, bool)
                or not isinstance(selected_id, int)
                or selected_id <= 0
            ):
                raise invalid
            from django.apps import apps as django_apps

            from core.role_principals import PRINCIPAL_MODELS, RolePrincipal

            profile = (
                django_apps.get_model(PRINCIPAL_MODELS[selected_kind])
                .objects.filter(pk=selected_id, is_active=True, user__is_active=True)
                .select_related("user")
                .first()
            )
            if profile is None:
                raise invalid
            assignee_id = profile.user_id
            principal = RolePrincipal(
                kind=selected_kind,
                principal_id=selected_id,
                user_id=assignee_id,
            )
        if assignee_id is None:  # malformed internal DTO; public parsing already rejects it
            raise invalid
        from apps.access.models import AccountType
        from core.permissions import Role, role_memberships_with_permission

        memberships = role_memberships_with_permission("tasks:read").filter(user_id=assignee_id)
        if branch_id is not None:
            memberships = memberships.filter(branch_id=branch_id)
        if department_id is not None:
            from django.db.models import Q

            memberships = memberships.filter(Q(department_id=None) | Q(department_id=department_id))
        rows = list(memberships.select_related("user", "account_type"))
        if not rows:
            raise invalid
        if principal is None:
            try:
                principal = resolve_unambiguous_user_principal(
                    assignee_id,
                    allowed_kinds=STAFF_PRINCIPAL_KINDS,
                    field="assignee",
                    message=_("Choose active task staff in the task's scope."),
                )
            except ValidationException:
                raise invalid from None
        if (expected_kind and principal.kind != expected_kind) or (
            expected_id is not None and principal.principal_id != expected_id
        ):
            raise invalid

        legacy_kind = {Role.TEACHER: AccountType.AccountKind.TEACHER}
        roles: set[str] = set()
        user = None
        for membership in rows:
            kind = (
                membership.account_type.account_kind
                if membership.account_type_id is not None
                else legacy_kind.get(membership.role, AccountType.AccountKind.STAFF)
            )
            if str(kind) != principal.kind:
                continue
            roles.add(
                membership.account_type.compatibility_role
                if membership.account_type_id is not None
                else membership.role
            )
            user = membership.user
        if user is None or not roles:
            raise invalid
        return TaskAssignee(
            user=user,
            principal_kind=principal.kind,
            principal_id=principal.principal_id,
            roles=frozenset(roles),
        )

    @staticmethod
    def _resolve_department(
        department_id: int | None,
        *,
        required: bool = False,
        allowed_grants: tuple[MembershipGrantScope, ...] | None = None,
    ):
        if department_id is None:
            if required:
                raise ValidationException(
                    _("A department is required."),
                    code="validation_error",
                    fields={"department": ["This field is required."]},
                )
            return None
        from apps.org.models import Department

        departments = Department.objects.select_related("branch").filter(
            pk=department_id,
            is_active=True,
            branch__is_active=True,
            branch__archived_at__isnull=True,
        )
        if allowed_grants is not None:
            from django.db.models import Q

            branch_ids = {grant.branch_id for grant in allowed_grants if grant.department_id is None}
            department_ids = {
                grant.department_id for grant in allowed_grants if grant.department_id is not None
            }
            departments = departments.filter(Q(branch_id__in=branch_ids) | Q(pk__in=department_ids))
        department = departments.first()
        if department is None:
            if allowed_grants is not None:
                raise PermissionException(
                    _("The task is outside your task scope."),
                    code="out_of_scope",
                )
            raise ValidationException(
                _("Invalid department."),
                code="validation_error",
                fields={"department": ["Not found."]},
            )
        return department

    @staticmethod
    def _resolve_branch(
        branch_id: int | None,
        *,
        allowed_branch_ids: set[int] | None = None,
    ):
        if branch_id is None:
            return None
        from apps.org.models import Branch

        branches = Branch.objects.filter(
            pk=branch_id,
            is_active=True,
            archived_at__isnull=True,
        )
        if allowed_branch_ids is not None:
            branches = branches.filter(pk__in=allowed_branch_ids)
        branch = branches.first()
        if branch is None:  # mirrors the old serializer's non-archived branch queryset
            if allowed_branch_ids is not None:
                raise PermissionException(
                    _("The task is outside your task scope."),
                    code="out_of_scope",
                )
            raise ValidationException(
                _("Invalid branch."), code="validation_error", fields={"branch": ["Not found."]}
            )
        return branch
