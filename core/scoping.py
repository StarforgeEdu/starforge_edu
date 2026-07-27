"""Branch/department object scoping for the layered (plain-view) style.

Mirrors ``core.permissions.ObjectScopedPermission``: a superuser or DIRECTOR sees
everything; otherwise a list is filtered to the caller's role-membership branches and
a single object outside them is a 403. Use in the repository/service list query and at
the detail/write boundary so a branch-scoped role can't read or mutate another branch."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from core.permissions import Role, get_role_memberships


def is_unscoped(request: Any) -> bool:
    """True when the caller bypasses branch scoping (superuser or DIRECTOR)."""
    if getattr(getattr(request, "user", None), "is_superuser", False):
        return True
    return any(m.role == Role.DIRECTOR for m in get_role_memberships(request))


def branch_ids(request: Any) -> set[int]:
    return {m.branch_id for m in get_role_memberships(request) if m.branch_id}


def department_ids(request: Any) -> set[int]:
    return {m.department_id for m in get_role_memberships(request) if m.department_id}


def scope_to_branches(request: Any, queryset: QuerySet, *, field: str = "branch_id") -> QuerySet:
    """Filter ``queryset`` to the caller's branches (no-op for an unscoped caller)."""
    if is_unscoped(request):
        return queryset
    return queryset.filter(**{f"{field}__in": branch_ids(request)})


def assert_in_branch_scope(request: Any, obj: Any) -> None:
    """403 if ``obj`` (with ``branch_id``) is outside the caller's branches."""
    if is_unscoped(request):
        return
    if getattr(obj, "branch_id", None) not in branch_ids(request):
        from core.exceptions import PermissionException

        raise PermissionException(code="out_of_scope")


def assert_branch_id_in_scope(request: Any, branch_id: int | None) -> None:
    """403 if ``branch_id`` is outside the caller's branches — for CREATE, where there
    is no object yet (a branch-scoped role must not create rows in another branch)."""
    if is_unscoped(request):
        return
    if branch_id not in branch_ids(request):
        from core.exceptions import PermissionException

        raise PermissionException(code="out_of_scope")
