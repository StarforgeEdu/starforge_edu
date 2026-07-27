"""Tasks + hierarchy services (F5-2/3).

Domain functions live here (imported by the layered services in ``services/v1``). They
hold the transactional core: the hierarchy gate (``can_assign``/``user_grade``), the
status lifecycle, and the fair auto-split.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.tasks.models import RoleGrade, Task
from apps.users.models import RoleMembership, User
from core.exceptions import PermissionException, UnprocessableEntity
from core.permissions import Role, has_permission_code

_UNSET: object = object()

# A staff member's "current load" for balancing = their not-yet-finished tasks.
_OPEN_LOAD_STATUSES = (Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.BLOCKED)
# Only staff can be tasked — never a student/parent who could never see it (matches
# the assignee queryset on TaskCreate/TaskAssign serializers).
_TASKABLE_ROLES = tuple(r for r in Role.ALL if r not in (Role.STUDENT, Role.PARENT))

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
    return {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}


def user_grade(user, roles: set[str] | None = None) -> int:
    """The seniority level of `user` = the max RoleGrade.level over their roles.
    Ungraded roles count as 0 (most junior)."""
    if roles is None:
        roles = _roles_of(user)
    grades = dict(RoleGrade.objects.values_list("role", "level"))
    return max((grades.get(r, 0) for r in roles), default=0)


def can_assign(*, actor, actor_roles: set[str], target_user) -> bool:
    """Hierarchy gate (F5-3): you may assign to an equal/lower grade, unless you
    hold tasks:assign_any (manager/CEO bypass) or are a superuser.

    Fails CLOSED on a partially-configured hierarchy: once any RoleGrade exists, a
    target whose roles are all UNGRADED is treated as unrankable — you may not task
    them without the bypass (so forgetting to grade a senior role can't be exploited
    to task them). An empty grade table means "no hierarchy configured" → unrestricted.
    """
    if getattr(actor, "is_superuser", False):
        return True
    if has_permission_code(actor_roles, "tasks:assign_any"):
        return True
    grades = dict(RoleGrade.objects.values_list("role", "level"))
    if not grades:
        return True  # hierarchy not configured for this center
    actor_grade = max((grades.get(r, 0) for r in actor_roles), default=0)
    target_levels = [grades[r] for r in _roles_of(target_user) if r in grades]
    if not target_levels:
        return False  # target unplaced in the hierarchy -> fail closed
    return actor_grade >= max(target_levels)


def _guard_assignee(actor, actor_roles, assignee) -> None:
    if assignee is not None and not can_assign(actor=actor, actor_roles=actor_roles, target_user=assignee):
        raise PermissionException(
            _("You can only assign tasks to an equal or lower grade."), code="cannot_assign_grade"
        )


@transaction.atomic
def create_task(
    *,
    title: str,
    created_by,
    created_by_roles: set[str],
    assignee=None,
    department=None,
    branch=None,
    description: str = "",
    priority: str = Task.Priority.NORMAL,
    due_at=None,
) -> Task:
    _guard_assignee(created_by, created_by_roles, assignee)
    return Task.objects.create(
        title=title,
        description=description,
        assignee=assignee,
        department=department,
        branch=branch,
        priority=priority,
        due_at=due_at,
        created_by=created_by,
    )


@transaction.atomic
def assign_task(*, task: Task, actor, actor_roles: set[str], assignee=_UNSET, department=_UNSET) -> Task:
    """Reassign a task to a person and/or a department. The person assignment is
    hierarchy-gated; clearing an assignee (None) is always allowed."""
    fields: list[str] = []
    if assignee is not _UNSET:
        _guard_assignee(actor, actor_roles, assignee)
        task.assignee = assignee
        fields.append("assignee")
    if department is not _UNSET:
        task.department = department
        fields.append("department")
    if fields:
        fields.append("updated_at")
        task.save(update_fields=fields)
    return task


@transaction.atomic
def transition_task(*, task: Task, to_status: str, actor=None) -> Task:
    # Re-fetch under a row lock so two concurrent transitions can't both read the
    # same pre-image and each pass the gate, bypassing the state-machine graph
    # (e.g. one racer commits OPEN->CANCELLED while the other commits OPEN->DONE,
    # landing a CANCELLED task in DONE). Mirrors every sibling transition service.
    task = Task.objects.select_for_update().get(pk=task.pk)
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
def auto_split_tasks(*, task_ids, department, actor, actor_roles: set[str], mode: str = "fair") -> dict:
    """F5-4: distribute a department's OPEN tasks across its staff. `fair` balances by
    current open-task load (each task goes to the least-loaded eligible person, then
    that person's load is bumped so a batch spreads evenly — a transparent rule, NOT a
    black box); `free` leaves them department-claimable (clears any assignee). Person
    assignment is hierarchy-gated (can_assign) so you can't auto-task above your grade."""
    tasks = list(
        Task.objects.select_for_update().filter(
            id__in=task_ids, department=department, status=Task.Status.OPEN
        )
    )
    if not tasks:
        raise UnprocessableEntity(_("No open tasks in that department to distribute."), code="no_open_tasks")

    if mode == "free":
        freed = 0
        for task in tasks:
            if task.assignee_id is not None:
                task.assignee = None
                task.save(update_fields=["assignee", "updated_at"])
                freed += 1
        return {"mode": "free", "assigned": 0, "freed": freed, "assignments": []}

    # fair: balance across the department's active, taskable staff the actor may assign
    # to (same who-can-be-tasked definition as the manual assign/create paths).
    staff_by_id: dict[int, User] = {}
    for membership in RoleMembership.objects.filter(
        department=department,
        revoked_at__isnull=True,
        user__is_active=True,
        role__in=_TASKABLE_ROLES,
    ).select_related("user"):
        staff_by_id.setdefault(membership.user_id, membership.user)
    eligible = [
        user
        for user in staff_by_id.values()
        if can_assign(actor=actor, actor_roles=actor_roles, target_user=user)
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
    for row in (
        Task.objects.filter(assignee_id__in=load, status__in=_OPEN_LOAD_STATUSES)
        .exclude(id__in=batch_ids)
        .values("assignee_id")
        .annotate(n=Count("id"))
    ):
        load[row["assignee_id"]] = row["n"]

    assignments = []
    for task in tasks:
        target = min(eligible, key=lambda user: (load[user.id], user.id))
        task.assignee = target
        task.save(update_fields=["assignee", "updated_at"])
        load[target.id] += 1
        assignments.append({"task": task.id, "assignee": target.id})
    return {"mode": "fair", "assigned": len(assignments), "freed": 0, "assignments": assignments}
