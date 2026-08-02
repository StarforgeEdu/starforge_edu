"""Tasks + hierarchy services (F5-2/3).

Domain functions live here (imported by the layered services in ``services/v1``). They
hold the transactional core: the hierarchy gate (``can_assign``/``user_grade``), the
status lifecycle, and the fair auto-split.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.tasks.models import RoleGrade, Task
from apps.users.models import User
from core.exceptions import PermissionException, UnprocessableEntity, ValidationException

_UNSET: object = object()

# A staff member's "current load" for balancing = their not-yet-finished tasks.
_OPEN_LOAD_STATUSES = (Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.BLOCKED)
MAX_TASK_DESCRIPTION_CHARS = 20_000
# Only staff can be tasked — never a student/parent who could never see it (matches
# the assignee queryset on TaskCreate/TaskAssign serializers).

# Allowed status transitions. DONE may still be cancelled; both DONE and CANCELLED
# can be reopened to OPEN. A same-status transition is a no-op (handled below).
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    Task.Status.OPEN: {Task.Status.IN_PROGRESS, Task.Status.BLOCKED, Task.Status.DONE, Task.Status.CANCELLED},
    Task.Status.IN_PROGRESS: {Task.Status.OPEN, Task.Status.BLOCKED, Task.Status.DONE, Task.Status.CANCELLED},
    Task.Status.BLOCKED: {Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.DONE, Task.Status.CANCELLED},
    Task.Status.DONE: {Task.Status.OPEN, Task.Status.CANCELLED},
    Task.Status.CANCELLED: {Task.Status.OPEN},
}


def _roles_of(user) -> set[str]:
    from core.permissions import get_unambiguous_user_roles

    return get_unambiguous_user_roles(user)


def user_grade(user, roles: set[str] | None = None, *, grades: dict[str, int] | None = None) -> int:
    """The seniority level of `user` = the max RoleGrade.level over their roles.
    Ungraded roles count as 0 (most junior)."""
    if roles is None:
        roles = _roles_of(user)
    if grades is None:
        grades = dict(RoleGrade.objects.values_list("role", "level"))
    return max((grades.get(r, 0) for r in roles), default=0)


def can_assign(
    *,
    actor,
    actor_roles: set[str],
    target_user,
    target_roles: set[str] | None = None,
    can_assign_any: bool = False,
) -> bool:
    """Hierarchy gate (F5-3): you may assign to an equal/lower grade, unless you
    hold tasks:assign_any (manager/CEO bypass) or are a superuser.

    Fails CLOSED on a partially-configured hierarchy: once any RoleGrade exists, a
    target whose roles are all UNGRADED is treated as unrankable — you may not task
    them without the bypass (so forgetting to grade a senior role can't be exploited
    to task them). An empty grade table means "no hierarchy configured" → unrestricted.
    """
    if getattr(actor, "is_superuser", False):
        return True
    if can_assign_any:
        return True
    grades = dict(RoleGrade.objects.values_list("role", "level"))
    if not grades:
        return True  # hierarchy not configured for this center
    actor_grade = user_grade(actor, actor_roles, grades=grades)
    target_levels = [grades[r] for r in (target_roles or _roles_of(target_user)) if r in grades]
    if not target_levels:
        return False  # target unplaced in the hierarchy -> fail closed
    return actor_grade >= max(target_levels)


def _guard_assignee(
    actor,
    actor_roles,
    assignee,
    *,
    assignee_roles: set[str] | None = None,
    can_assign_any: bool = False,
) -> None:
    if assignee is not None and not can_assign(
        actor=actor,
        actor_roles=actor_roles,
        target_user=assignee,
        target_roles=assignee_roles,
        can_assign_any=can_assign_any,
    ):
        raise PermissionException(
            _("You can only assign tasks to an equal or lower grade."), code="cannot_assign_grade"
        )


@transaction.atomic
def create_task(
    *,
    title: str,
    created_by,
    created_by_principal=None,
    created_by_roles: set[str],
    assignee=None,
    assignee_principal_kind: str = "",
    assignee_principal_id: int | None = None,
    assignee_roles: set[str] | None = None,
    can_assign_any: bool = False,
    department=None,
    branch=None,
    description: str = "",
    priority: str = Task.Priority.NORMAL,
    due_at=None,
) -> Task:
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValidationException(
            _("Invalid task title."),
            code="validation_error",
            fields={"title": [_("Use between 1 and 200 characters.")]},
        )
    if not isinstance(description, str) or len(description) > MAX_TASK_DESCRIPTION_CHARS:
        raise ValidationException(
            _("The task description is too long."),
            code="validation_error",
            fields={"description": [_("Must be at most 20000 characters.")]},
        )
    if priority not in Task.Priority.values:
        raise ValidationException(
            _("Invalid task priority."),
            code="validation_error",
            fields={"priority": [_("Choose a supported priority.")]},
        )
    if assignee is None:
        if assignee_principal_kind or assignee_principal_id is not None:
            raise ValidationException(
                _("Invalid assignee attribution."),
                code="validation_error",
                fields={"assignee": [_("Clear the assignee and its role attribution together.")]},
            )
    elif (
        assignee_principal_kind not in {"staff", "teacher"}
        or not isinstance(assignee_principal_id, int)
        or isinstance(assignee_principal_id, bool)
        or assignee_principal_id <= 0
    ):
        raise ValidationException(
            _("Invalid assignee attribution."),
            code="validation_error",
            fields={"assignee": [_("Choose one active staff role account.")]},
        )
    _guard_assignee(
        created_by,
        created_by_roles,
        assignee,
        assignee_roles=assignee_roles,
        can_assign_any=can_assign_any,
    )
    if created_by is None:
        if created_by_principal is not None:
            raise ValidationException(
                _("Invalid task creator attribution."),
                code="validation_error",
                fields={"created_by": [_("Clear the creator and its role attribution together.")]},
            )
        creator_fields: dict[str, Any] = {
            "created_by_attribution_status": Task.CreatorAttributionStatus.QUARANTINED,
        }
    else:
        if created_by_principal is None:
            from core.role_principals import STAFF_PRINCIPAL_KINDS, resolve_unambiguous_user_principal

            created_by_principal = resolve_unambiguous_user_principal(
                created_by.pk,
                allowed_kinds=STAFF_PRINCIPAL_KINDS,
                field="created_by",
                message=_("The task creator does not identify one active staff role account."),
            )
        if (
            created_by_principal.kind not in {"staff", "teacher"}
            or created_by_principal.user_id != created_by.pk
        ):
            raise ValidationException(
                _("Invalid task creator attribution."),
                code="validation_error",
                fields={"created_by": [_("Choose the active creator role account.")]},
            )
        creator_fields = {
            "created_by_principal_kind": created_by_principal.kind,
            "created_by_principal_id": created_by_principal.principal_id,
            "created_by_attribution_status": Task.CreatorAttributionStatus.CAPTURED,
        }
    return Task.objects.create(
        title=title,
        description=description,
        assignee=assignee,
        assignee_principal_kind=assignee_principal_kind,
        assignee_principal_id=assignee_principal_id,
        department=department,
        branch=branch,
        priority=priority,
        due_at=due_at,
        created_by=created_by,
        **creator_fields,
    )


@transaction.atomic
def assign_task(
    *,
    task: Task,
    actor,
    actor_roles: set[str],
    assignee=_UNSET,
    assignee_principal_kind=_UNSET,
    assignee_principal_id=_UNSET,
    assignee_roles: set[str] | None = None,
    can_assign_any: bool = False,
    department=_UNSET,
    branch=_UNSET,
) -> Task:
    """Reassign a task to a person and/or a department. The person assignment is
    hierarchy-gated; clearing an assignee (None) is always allowed."""
    fields: list[str] = []
    if assignee is not _UNSET:
        if assignee is None:
            if assignee_principal_kind not in (_UNSET, "") or assignee_principal_id not in (
                _UNSET,
                None,
            ):
                raise ValidationException(
                    _("Invalid assignee attribution."),
                    code="validation_error",
                    fields={"assignee": [_("Clear the assignee and its role attribution together.")]},
                )
        elif (
            assignee_principal_kind not in {"staff", "teacher"}
            or not isinstance(assignee_principal_id, int)
            or isinstance(assignee_principal_id, bool)
            or assignee_principal_id <= 0
        ):
            raise ValidationException(
                _("Invalid assignee attribution."),
                code="validation_error",
                fields={"assignee": [_("Choose one active staff role account.")]},
            )
        _guard_assignee(
            actor,
            actor_roles,
            assignee,
            assignee_roles=assignee_roles,
            can_assign_any=can_assign_any,
        )
        task.assignee = assignee
        fields.append("assignee")
        task.assignee_principal_kind = "" if assignee_principal_kind is _UNSET else assignee_principal_kind
        task.assignee_principal_id = None if assignee_principal_id is _UNSET else assignee_principal_id
        task.assignee_attribution_status = "captured"
        fields.extend(["assignee_principal_kind", "assignee_principal_id", "assignee_attribution_status"])
    if department is not _UNSET:
        task.department = department
        fields.append("department")
    if branch is not _UNSET:
        task.branch = branch
        fields.append("branch")
    if fields:
        fields.append("updated_at")
        task.save(update_fields=fields)
    return task


@transaction.atomic
def transition_task(
    *,
    task: Task,
    to_status: str,
    actor,
    actor_principal_kind: str | None = None,
    actor_principal_id: int | None = None,
    can_transition_any: bool = False,
) -> Task:
    # Re-fetch under a row lock so two concurrent transitions can't both read the
    # same pre-image and each pass the gate, bypassing the state-machine graph
    # (e.g. one racer commits OPEN->CANCELLED while the other commits OPEN->DONE,
    # landing a CANCELLED task in DONE). Mirrors every sibling transition service.
    task = Task.objects.select_for_update().get(pk=task.pk)
    if actor_principal_kind is None or actor_principal_id is None:
        from core.role_principals import STAFF_PRINCIPAL_KINDS, resolve_unambiguous_user_principal

        principal = resolve_unambiguous_user_principal(
            actor.id,
            allowed_kinds=STAFF_PRINCIPAL_KINDS,
            field="actor",
            message=_("The actor does not identify one active staff role account."),
        )
        actor_principal_kind, actor_principal_id = principal.kind, principal.principal_id
    is_assignee = (
        task.assignee_attribution_status == "captured"
        and task.assignee_principal_kind == actor_principal_kind
        and task.assignee_principal_id == actor_principal_id
    )
    if not is_assignee and not can_transition_any:
        raise PermissionException(_("Only the assignee may transition this task."), code="not_task_assignee")
    if to_status == task.status:
        return task  # no-op
    if to_status not in _ALLOWED_TRANSITIONS.get(task.status, set()):
        raise UnprocessableEntity(
            _("That status change is not allowed from the task's current state."),
            code="invalid_transition",
        )
    task.status = to_status
    task.completed_at = timezone.now() if to_status == Task.Status.DONE else None
    task.save(update_fields=["status", "completed_at", "updated_at"])
    return task


@transaction.atomic
def auto_split_tasks(
    *,
    task_ids,
    department,
    actor,
    actor_roles: set[str],
    can_assign_any: bool = False,
    mode: str = "fair",
) -> dict:
    """F5-4: distribute a department's OPEN tasks across its staff. `fair` balances by
    current open-task load (each task goes to the least-loaded eligible person, then
    that person's load is bumped so a batch spreads evenly — a transparent rule, NOT a
    black box); `free` leaves them department-claimable (clears any assignee). Person
    assignment is hierarchy-gated (can_assign) so you can't auto-task above your grade."""
    if mode not in {"fair", "free"}:
        raise ValidationException(
            _("Invalid distribution mode."),
            code="validation_error",
            fields={"mode": [_("Choose fair or free.")]},
        )
    if not task_ids:
        raise UnprocessableEntity(_("No open tasks in that department to distribute."), code="no_open_tasks")

    # Serialize all distribution runs for a department. Locking only the selected
    # task rows lets two disjoint batches observe the same pre-run load and both
    # choose the same worker, defeating the fairness guarantee.
    department = type(department).objects.select_for_update().get(pk=department.pk)
    tasks = list(
        Task.objects.select_for_update()
        .filter(
            id__in=task_ids,
            department=department,
            branch_id=department.branch_id,
            status=Task.Status.OPEN,
        )
        .order_by("id")
    )
    if len(tasks) != len(set(task_ids)):
        raise UnprocessableEntity(_("No open tasks in that department to distribute."), code="no_open_tasks")

    if mode == "free":
        changed: list[Task] = []
        changed_at = timezone.now()
        for task in tasks:
            if task.assignee_id is not None:
                task.assignee = None
                task.assignee_principal_kind = ""
                task.assignee_principal_id = None
                task.assignee_attribution_status = "captured"
                task.updated_at = changed_at
                changed.append(task)
        if changed:
            Task.objects.bulk_update(
                changed,
                [
                    "assignee",
                    "assignee_principal_kind",
                    "assignee_principal_id",
                    "assignee_attribution_status",
                    "updated_at",
                ],
                batch_size=500,
            )
        return {"mode": "free", "assigned": 0, "freed": len(changed), "assignments": []}

    # fair: balance across the department's active, taskable staff the actor may assign
    # to (same who-can-be-tasked definition as the manual assign/create paths).
    staff_by_id: dict[int, User] = {}
    from apps.access.models import AccountType
    from core.permissions import Role, role_memberships_with_permission
    from core.role_principals import STAFF_PRINCIPAL_KINDS, find_unambiguous_user_principals

    memberships = list(
        role_memberships_with_permission("tasks:read")
        .filter(branch_id=department.branch_id)
        .filter(department_id=department.pk)
        .select_related("user", "account_type")
    )
    user_ids = {membership.user_id for membership in memberships}
    principals = find_unambiguous_user_principals(
        user_ids,
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
    )
    legacy_kind = {Role.TEACHER: AccountType.AccountKind.TEACHER}
    roles_by_user: dict[int, set[str]] = {}
    principal_by_user = {}
    for membership in memberships:
        principal = principals.get(membership.user_id)
        kind = (
            membership.account_type.account_kind
            if membership.account_type_id is not None
            else legacy_kind.get(membership.role, AccountType.AccountKind.STAFF)
        )
        if principal is None or principal.kind != str(kind):
            continue
        staff_by_id[membership.user_id] = membership.user
        principal_by_user[membership.user_id] = principal
        roles_by_user.setdefault(membership.user_id, set()).add(
            membership.account_type.compatibility_role
            if membership.account_type_id is not None
            else membership.role
        )
    eligible = [
        user
        for user in staff_by_id.values()
        if can_assign(
            actor=actor,
            actor_roles=actor_roles,
            target_user=user,
            target_roles=roles_by_user[user.id],
            can_assign_any=can_assign_any,
        )
    ]
    if not eligible:
        raise UnprocessableEntity(
            _("There is no department staff you are allowed to assign these tasks to."),
            code="no_eligible_staff",
        )

    # Seed each person's current load from their OTHER open work — the batch being
    # redistributed is NOT fixed load (else rebalancing an overloaded person's pile
    # would dump it all on an idle teammate, the inverse of balancing).
    batch_ids = [task.id for task in tasks]
    load = {user.id: 0 for user in eligible}
    exact_assignees = Q(pk__in=[])
    for user_id in load:
        principal = principal_by_user[user_id]
        exact_assignees |= Q(
            assignee_id=user_id,
            assignee_principal_kind=principal.kind,
            assignee_principal_id=principal.principal_id,
            assignee_attribution_status="captured",
        )
    for row in (
        Task.objects.filter(exact_assignees, department=department, status__in=_OPEN_LOAD_STATUSES)
        .exclude(id__in=batch_ids)
        .values("assignee_id")
        .annotate(n=Count("id"))
    ):
        load[row["assignee_id"]] = row["n"]

    assignments = []
    changed_at = timezone.now()
    for task in tasks:
        target = min(eligible, key=lambda user: (load[user.id], user.id))
        target_principal = principal_by_user[target.id]
        task.assignee = target
        task.assignee_principal_kind = target_principal.kind
        task.assignee_principal_id = target_principal.principal_id
        task.assignee_attribution_status = "captured"
        task.updated_at = changed_at
        load[target.id] += 1
        assignments.append({"task": task.id, "assignee": target.id})
    Task.objects.bulk_update(
        tasks,
        [
            "assignee",
            "assignee_principal_kind",
            "assignee_principal_id",
            "assignee_attribution_status",
            "updated_at",
        ],
        batch_size=500,
    )
    return {"mode": "fair", "assigned": len(assignments), "freed": 0, "assignments": assignments}
