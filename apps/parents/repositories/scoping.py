"""Exact permission-bearing row scope for the parent domain (TD-5).

Organization-wide grants see every row. Scoped staff are constrained by the
branch/department on the membership granting the requested permission, while a
parent sees only their own rows through the sanctioned family relationship.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES
from core.permissions import PermissionRoleSet, Role
from core.scoping import (
    permission_membership_is_unscoped,
    permission_membership_scope_q,
    permission_membership_scopes,
    role_membership_scope_q,
)

SCOPED_STAFF_ROLES = frozenset({Role.HEAD_OF_DEPT, Role.REGISTRAR, Role.IT})


def attributed_unassigned_parent_scope_q(*, user, roles, permission: str) -> Q:
    """Creation boundaries a scoped operator may use before a guardian link.

    Unresolved/conflicting legacy profiles never match. The permission predicate
    is paired to the exact membership that grants ``permission`` so another
    branch cannot borrow an unrelated account assignment.
    """
    if getattr(user, "is_superuser", False):
        return Q()
    role_set = set(roles or ())
    if isinstance(roles, PermissionRoleSet):
        # Organization-wide staff can review unresolved historical profiles and
        # intentional owner-created drafts. Scoped staff must instead prove the
        # immutable captured boundary below.
        if permission_membership_is_unscoped(
            roles=roles,
            permission=permission,
            account_kinds={"staff"},
        ):
            return Q()
        boundary = permission_membership_scope_q(
            roles=roles,
            permission=permission,
            branch_field="branch_at_creation_id",
            department_field="department_at_creation_id",
            account_kinds={"staff"},
        )
    else:
        if Role.DIRECTOR in role_set:
            return Q()
        scoped_staff = role_set & SCOPED_STAFF_ROLES
        boundary = role_membership_scope_q(
            user=user,
            roles=scoped_staff,
            branch_field="branch_at_creation_id",
            department_field="department_at_creation_id",
        )
    # The caller applies this predicate only to rows annotated as having no
    # active Guardian. Revoked links remain as history and must not prevent the
    # immutable creation attribution from governing the unassigned profile.
    return Q(attribution_status__in=ATTRIBUTED_SCOPE_STATUSES) & boundary


def scope_rows(
    qs: QuerySet,
    *,
    user,
    roles,
    permission: str,
    own_filter: dict,
    branch_field: str,
    department_field: str,
) -> QuerySet:
    if getattr(user, "is_superuser", False):
        return qs
    role_set = set(roles or ())
    if Role.DIRECTOR in role_set and not isinstance(roles, PermissionRoleSet):
        return qs

    visible = Q(pk__in=[])
    # Plain sets exist only in direct legacy domain tests and cannot carry the
    # permission-to-membership pairing used by request paths. Preserve their
    # former role-scoped behavior without weakening PermissionRoleSet handling.
    if not isinstance(roles, PermissionRoleSet):
        scoped_staff = role_set & SCOPED_STAFF_ROLES
        if scoped_staff:
            visible |= role_membership_scope_q(
                user=user,
                roles=scoped_staff,
                branch_field=branch_field,
                department_field=department_field,
            )
    visible |= permission_membership_scope_q(
        roles=roles,
        permission=permission,
        branch_field=branch_field,
        department_field=department_field,
        account_kinds={"staff"},
    )
    parent_self_service = (
        any(
            scope.account_kind == "parent"
            for scope in permission_membership_scopes(
                roles=roles,
                permission=permission,
                account_kinds={"parent"},
            )
        )
        if isinstance(roles, PermissionRoleSet)
        else Role.PARENT in role_set
    )
    if parent_self_service:
        visible |= Q(**own_filter)
    return qs.filter(visible).distinct()
