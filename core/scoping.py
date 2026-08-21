"""Branch/department object scoping for the layered (plain-view) style.

A superuser or DIRECTOR sees everything; otherwise a list is filtered to the caller's
role-membership branches and a single object outside them is a 403. Use this in the
repository/service list query and at the detail/write boundary so a branch-scoped role
cannot read or mutate another branch."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db.models import Q, QuerySet

from core.permissions import (
    MembershipGrantScope,
    PermissionRoleSet,
    _code_allowed,
    get_role_memberships,
    get_user_roles,
    has_permission_code,
)


def is_unscoped(request: Any) -> bool:
    """True for superusers and canonical organization-owner memberships.

    The legacy ``role`` column is not trusted when an AccountType exists.  This
    prevents a stale/malformed ``role='director'`` value on a custom account type
    from lending global scope to unrelated grants.
    """
    if getattr(getattr(request, "user", None), "is_superuser", False):
        return True
    return any(scope.is_organization_wide for scope in get_user_roles(request).membership_scopes)


def permission_membership_is_unscoped(
    *,
    roles: Iterable[str],
    permission: str,
    account_kinds: Iterable[str] | None = None,
) -> bool:
    """Whether an organization-wide membership supplies this exact permission.

    A director assignment is an organization-wide boundary, but it must not lend
    that boundary to a permission granted by a different membership.  Keeping the
    check permission-specific closes that cross-membership scope escalation while
    preserving the intended director behavior.
    """
    return any(
        membership.is_organization_wide
        for membership in permission_membership_scopes(
            roles=roles,
            permission=permission,
            account_kinds=account_kinds,
        )
    )


def is_permission_unscoped(
    request: Any,
    *,
    permission: str,
    account_kinds: Iterable[str] | None = None,
) -> bool:
    """Request form of :func:`permission_membership_is_unscoped`."""
    if getattr(getattr(request, "user", None), "is_superuser", False):
        return True
    return permission_membership_is_unscoped(
        roles=get_user_roles(request),
        permission=permission,
        account_kinds=account_kinds,
    )


def assert_permission_organization_scope(
    request: Any,
    *,
    permission: str,
    account_kinds: Iterable[str] | None = None,
) -> None:
    """Require this exact permission to come from an organization-wide grant.

    Use this for tenant-global configuration that cannot be safely narrowed to a
    branch or department (for example, editing account-type permission sets).
    """
    if is_permission_unscoped(
        request,
        permission=permission,
        account_kinds=account_kinds,
    ):
        return
    from core.exceptions import PermissionException

    raise PermissionException(code="out_of_scope")


def branch_ids(request: Any) -> set[int]:
    return {m.branch_id for m in get_role_memberships(request) if m.branch_id}


def department_ids(request: Any) -> set[int]:
    return {m.department_id for m in get_role_memberships(request) if m.department_id}


def role_membership_scope_q(
    *,
    user: Any,
    roles: Iterable[str],
    branch_field: str,
    department_field: str | None = None,
) -> Q:
    """Build a fail-closed row-scope for active role memberships.

    A membership without a department grants its whole branch. A membership with
    a department grants only rows in that exact branch/department pair when the
    target model exposes a department path. For branch-only resources, the most
    precise enforceable boundary remains the membership's branch.

    ``roles`` is deliberately explicit: callers can union this predicate with
    another role's natural scope (for example, a user who is both a department HoD
    and a teacher still sees lessons they personally teach).
    """
    role_set = set(roles)
    if not role_set or not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])

    memberships = list(
        user.role_memberships.filter(
            revoked_at__isnull=True,
            role__in=role_set,
        )
        .filter(Q(account_type__isnull=True) | Q(account_type__is_active=True))
        .values_list("branch_id", "department_id")
    )
    if not memberships:
        return Q(pk__in=[])

    branch_wide = {branch_id for branch_id, department_id in memberships if department_id is None}
    scoped = Q(**{f"{branch_field}__in": branch_wide}) if branch_wide else Q(pk__in=[])
    for branch_id, department_id in memberships:
        if department_id is None:
            continue
        if department_field is None:
            scoped |= Q(**{branch_field: branch_id})
        else:
            scoped |= Q(
                **{
                    branch_field: branch_id,
                    department_field: department_id,
                }
            )
    return scoped


def permission_membership_scope_q(
    *,
    roles: Iterable[str],
    permission: str,
    branch_field: str,
    department_field: str | None = None,
    account_kinds: Iterable[str] | None = None,
) -> Q:
    """Scope rows to memberships that actually grant ``permission``.

    This is the canonical bridge for custom account types: a counselor grant in
    Branch A cannot accidentally borrow the user's unrelated Branch B account
    type. Legacy/null memberships retain matrix+override compatibility.
    """
    memberships = permission_membership_scopes(
        roles=roles,
        permission=permission,
        account_kinds=account_kinds,
    )
    # ``Q()`` is neutral when OR-ed with another predicate, so use an explicit
    # always-true row predicate for an exact permission-bearing director scope.
    if any(membership.is_organization_wide for membership in memberships):
        return Q(pk__isnull=False)

    scoped = Q(pk__in=[])
    for membership in memberships:
        if membership.department_id is None or department_field is None:
            scoped |= Q(**{branch_field: membership.branch_id})
        else:
            scoped |= Q(
                **{
                    branch_field: membership.branch_id,
                    department_field: membership.department_id,
                }
            )
    return scoped


def permission_membership_scopes(
    *,
    roles: Iterable[str],
    permission: str,
    account_kinds: Iterable[str] | None = None,
) -> tuple[MembershipGrantScope, ...]:
    """Memberships that individually supply ``permission``.

    Request paths pass :class:`PermissionRoleSet`, whose per-membership grant
    metadata prevents a scope from one account type being borrowed by another.
    A plain role set intentionally has no trustworthy membership/grant pairing
    and therefore fails closed here.
    """
    if not isinstance(roles, PermissionRoleSet):
        return ()
    allowed_kinds = set(account_kinds) if account_kinds is not None else None
    matches: list[MembershipGrantScope] = []
    for membership in roles.membership_scopes:
        if allowed_kinds is not None and membership.account_kind not in allowed_kinds:
            continue
        allowed = (
            has_permission_code({membership.role}, permission)
            if membership.is_legacy_fallback
            else _code_allowed(set(membership.grants), set(), permission)
        )
        if allowed:
            matches.append(membership)
    return tuple(matches)


def permission_membership_branch_ids(
    *,
    roles: Iterable[str],
    permission: str,
    account_kinds: Iterable[str] | None = None,
) -> set[int]:
    """Branch ids from only the memberships granting ``permission``."""
    return {
        membership.branch_id
        for membership in permission_membership_scopes(
            roles=roles,
            permission=permission,
            account_kinds=account_kinds,
        )
    }


def permission_membership_branch_wide_ids(
    *,
    roles: Iterable[str],
    permission: str,
    account_kinds: Iterable[str] | None = None,
) -> set[int]:
    """Branch ids where ``permission`` is granted without a department limit.

    Use this for shared branch resources that have no department attribute with
    which to enforce a narrower membership (for example branch policy, rooms,
    or cross-branch transfer history). Organization-wide callers are handled by
    :func:`is_permission_unscoped` before this helper is used.
    """
    return {
        membership.branch_id
        for membership in permission_membership_scopes(
            roles=roles,
            permission=permission,
            account_kinds=account_kinds,
        )
        if membership.branch_id is not None
        and membership.department_id is None
        and not membership.is_organization_wide
    }


def permission_membership_department_ids(
    *,
    roles: Iterable[str],
    permission: str,
    account_kinds: Iterable[str] | None = None,
) -> set[int]:
    """Department ids from only the memberships granting ``permission``."""
    return {
        membership.department_id
        for membership in permission_membership_scopes(
            roles=roles,
            permission=permission,
            account_kinds=account_kinds,
        )
        if membership.department_id is not None
    }


def request_permission_membership_allows(
    request: Any,
    *,
    permission: str,
    branch_id: int | None,
    department_id: int | None = None,
    enforce_department: bool = True,
    account_kinds: Iterable[str] | None = None,
) -> bool:
    """Whether the exact membership granting ``permission`` covers the target."""
    if getattr(getattr(request, "user", None), "is_superuser", False):
        return True
    roles = get_user_roles(request)
    if not isinstance(roles, PermissionRoleSet):
        return False
    allowed_kinds = set(account_kinds) if account_kinds is not None else None
    for membership in roles.membership_scopes:
        if allowed_kinds is not None and membership.account_kind not in allowed_kinds:
            continue
        allowed = (
            has_permission_code({membership.role}, permission)
            if membership.is_legacy_fallback
            else _code_allowed(set(membership.grants), set(), permission)
        )
        if not allowed:
            continue
        if membership.is_organization_wide:
            return True
        if membership.branch_id != branch_id:
            continue
        if (
            enforce_department
            and membership.department_id is not None
            and membership.department_id != department_id
        ):
            continue
        return True
    return False


def assert_permission_membership_scope(
    request: Any,
    *,
    permission: str,
    branch_id: int | None,
    department_id: int | None = None,
    enforce_department: bool = True,
    account_kinds: Iterable[str] | None = None,
) -> None:
    if request_permission_membership_allows(
        request,
        permission=permission,
        branch_id=branch_id,
        department_id=department_id,
        enforce_department=enforce_department,
        account_kinds=account_kinds,
    ):
        return
    from core.exceptions import PermissionException

    raise PermissionException(code="out_of_scope")


def scope_to_permission_memberships(
    request: Any,
    queryset: QuerySet,
    *,
    permission: str,
    branch_field: str,
    department_field: str | None = None,
    account_kinds: Iterable[str] | None = None,
) -> QuerySet:
    """Filter rows by the exact memberships supplying ``permission``."""
    if is_permission_unscoped(
        request,
        permission=permission,
        account_kinds=account_kinds,
    ):
        return queryset
    return queryset.filter(
        permission_membership_scope_q(
            roles=get_user_roles(request),
            permission=permission,
            branch_field=branch_field,
            department_field=department_field,
            account_kinds=account_kinds,
        )
    )


def request_role_membership_allows(
    request: Any,
    *,
    roles: Iterable[str],
    branch_id: int | None,
    department_id: int | None = None,
) -> bool:
    """Whether one of ``request``'s active memberships covers an object.

    Directors/superusers retain their tenant-wide bypass. For other roles, a
    department-scoped membership never silently expands to its whole branch.
    """
    if is_unscoped(request):
        return True
    return role_memberships_allow(
        get_role_memberships(request),
        roles=roles,
        branch_id=branch_id,
        department_id=department_id,
    )


def role_memberships_allow(
    memberships: Iterable[Any],
    *,
    roles: Iterable[str],
    branch_id: int | None,
    department_id: int | None = None,
) -> bool:
    """Pure membership check shared by request and domain-service boundaries."""
    role_set = set(roles)
    for membership in memberships:
        if membership.role not in role_set or membership.branch_id != branch_id:
            continue
        if membership.department_id is None or membership.department_id == department_id:
            return True
    return False


def assert_in_role_membership_scope(
    request: Any,
    obj: Any,
    *,
    roles: Iterable[str],
    branch_attr: str = "branch_id",
    department_attr: str = "department_id",
) -> None:
    """403 unless ``obj`` is covered by an active branch/department membership."""
    if request_role_membership_allows(
        request,
        roles=roles,
        branch_id=getattr(obj, branch_attr, None),
        department_id=getattr(obj, department_attr, None),
    ):
        return
    from core.exceptions import PermissionException

    raise PermissionException(code="out_of_scope")


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
