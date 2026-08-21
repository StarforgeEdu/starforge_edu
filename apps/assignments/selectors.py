"""Assignments read selectors: role-scoped Assignment/Submission queries.

Drafts and other cohorts' assignments are filtered OUT of a student's queryset
(so they 404 on access, never a 403 that leaks existence)."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.assignments.models import Assignment, Submission
from core.permissions import PermissionRoleSet, Role, get_unambiguous_user_roles
from core.scoping import (
    permission_membership_is_unscoped,
    permission_membership_scope_q,
    permission_membership_scopes,
    role_membership_scope_q,
)

STAFF_ROLES = {Role.DIRECTOR}

# Natural relationships are intentionally narrower than the configurable
# permission catalogue.  An administrator may accidentally attach a write
# grant to a student account type, but that must not turn "is enrolled in this
# cohort" into authoring authority.  Staff permissions remain explicitly
# delegable through their membership boundary below.
_NATURAL_PERMISSIONS_BY_KIND = {
    "teacher": frozenset({"assignments:read", "assignments:write"}),
    "student": frozenset({"assignments:read", "assignments:submit"}),
}


def _cohorts_taught_by(
    user,
    *,
    roles: set[str] | None = None,
    permission: str = "assignments:read",
) -> QuerySet:
    from apps.cohorts.selectors import taught_cohorts

    cohorts = taught_cohorts(user=user)
    if isinstance(roles, PermissionRoleSet):
        cohorts = cohorts.filter(
            permission_membership_scope_q(
                roles=roles,
                permission=permission,
                branch_field="branch_id",
                department_field="department_id",
                account_kinds={"teacher"},
            )
        )
    return cohorts.values_list("id", flat=True)


def _kind_has_assignment_permission(
    roles: set[str],
    *,
    permission: str,
    kind: str,
    legacy_role: str,
) -> bool:
    if permission not in _NATURAL_PERMISSIONS_BY_KIND.get(kind, ()):
        return False
    if isinstance(roles, PermissionRoleSet):
        return bool(
            permission_membership_scopes(
                roles=roles,
                permission=permission,
                account_kinds={kind},
            )
        )
    return legacy_role in roles


def scoped_assignments(
    *,
    user,
    roles: set[str] | None = None,
    permission: str = "assignments:read",
) -> QuerySet[Assignment]:
    qs = Assignment.objects.select_related("cohort")
    if user.is_superuser:
        return qs
    if roles is None:
        roles = get_unambiguous_user_roles(user)
    if isinstance(roles, PermissionRoleSet):
        if permission_membership_is_unscoped(
            roles=roles,
            permission=permission,
            account_kinds={"staff"},
        ):
            return qs
    elif roles & STAFF_ROLES:
        return qs
    visible = permission_membership_scope_q(
        roles=roles,
        permission=permission,
        branch_field="cohort__branch_id",
        department_field="cohort__department_id",
        account_kinds={"staff"},
    )
    if not isinstance(roles, PermissionRoleSet) and Role.HEAD_OF_DEPT in roles:
        visible |= role_membership_scope_q(
            user=user,
            roles={Role.HEAD_OF_DEPT},
            branch_field="cohort__branch_id",
            department_field="cohort__department_id",
        )
    if _kind_has_assignment_permission(
        roles,
        permission=permission,
        kind="teacher",
        legacy_role=Role.TEACHER,
    ):  # natural ownership: cohorts this teacher actually teaches
        visible |= Q(
            cohort_id__in=_cohorts_taught_by(
                user,
                roles=roles,
                permission=permission,
            )
        )
    if _kind_has_assignment_permission(
        roles,
        permission=permission,
        kind="student",
        legacy_role=Role.STUDENT,
    ):  # published only, own cohorts
        visible |= Q(
            status=Assignment.Status.PUBLISHED,
            cohort__memberships__student__user=user,
            cohort__memberships__end_date__isnull=True,
        )
    return qs.filter(visible).distinct()


def scoped_submissions(
    *,
    user,
    roles: set[str] | None = None,
    permission: str = "assignments:read",
) -> QuerySet[Submission]:
    qs = Submission.objects.select_related("student__user", "assignment", "grade")
    if user.is_superuser:
        return qs
    if roles is None:
        roles = get_unambiguous_user_roles(user)
    if isinstance(roles, PermissionRoleSet):
        if permission_membership_is_unscoped(
            roles=roles,
            permission=permission,
            account_kinds={"staff"},
        ):
            return qs
    elif roles & STAFF_ROLES:
        return qs
    visible = permission_membership_scope_q(
        roles=roles,
        permission=permission,
        branch_field="assignment__cohort__branch_id",
        department_field="assignment__cohort__department_id",
        account_kinds={"staff"},
    )
    if not isinstance(roles, PermissionRoleSet) and Role.HEAD_OF_DEPT in roles:
        visible |= role_membership_scope_q(
            user=user,
            roles={Role.HEAD_OF_DEPT},
            branch_field="assignment__cohort__branch_id",
            department_field="assignment__cohort__department_id",
        )
    if _kind_has_assignment_permission(
        roles,
        permission=permission,
        kind="teacher",
        legacy_role=Role.TEACHER,
    ):  # natural ownership: submissions for taught cohorts
        visible |= Q(
            assignment__cohort_id__in=_cohorts_taught_by(
                user,
                roles=roles,
                permission=permission,
            )
        )
    if _kind_has_assignment_permission(
        roles,
        permission=permission,
        kind="student",
        legacy_role=Role.STUDENT,
    ):  # own submissions only
        visible |= Q(student__user=user)
    return qs.filter(visible).distinct()
