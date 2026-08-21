"""Permission-bound authorization primitives for the reports domain.

Report access is a *compound* grant: the ``reports`` permission for the
operation and the source domain's read grant must intersect at a compatible
membership boundary. Keeping that pairing at branch/department level prevents
a grant in one location from borrowing scope from an unrelated assignment.

The small plain-``set`` fallback exists for legacy direct-generator tests and
old internal callers only. HTTP requests and background workers use
``PermissionRoleSet`` values built from live memberships.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.permissions import (
    MembershipGrantScope,
    PermissionRoleSet,
    Role,
    _code_allowed,
    has_permission_code,
)

if TYPE_CHECKING:
    from apps.reports.models import Report


REPORT_DOMAIN_PERMISSION: dict[str, str] = {
    "enrollment": "students:read",
    "attendance": "attendance:read",
    "grades": "academics:read",
    "finance": "finance:read",
    "ai_usage": "ai:read",
    "storage_usage": "content:read",
}

# These source tables have no safe branch/department attribution. They remain
# organization-wide until their data models can prove a narrower scope.
ORGANIZATION_ONLY_REPORTS = frozenset({"ai_usage", "storage_usage"})
MAX_SCOPE_ITEMS = 1_000

SCOPE_VERSION = 3
SCOPE_VERSION_PARAM = "_scope_version"
SCOPE_ORGANIZATION_PARAM = "_scope_organization"
SCOPE_BRANCH_IDS_PARAM = "_scope_branch_ids"
SCOPE_DEPARTMENT_KEYS_PARAM = "_scope_department_keys"
SCOPE_TEACHER_KEYS_PARAM = "_scope_teacher_keys"
SCOPE_TEACHER_COHORT_IDS_PARAM = "_scope_teacher_cohort_ids"
SCOPE_PARAMS = frozenset(
    {
        SCOPE_VERSION_PARAM,
        SCOPE_ORGANIZATION_PARAM,
        SCOPE_BRANCH_IDS_PARAM,
        SCOPE_DEPARTMENT_KEYS_PARAM,
        SCOPE_TEACHER_KEYS_PARAM,
        SCOPE_TEACHER_COHORT_IDS_PARAM,
    }
)


def domain_permission(report_key: str) -> str | None:
    """Return the explicit data-domain permission for a report key."""
    return REPORT_DOMAIN_PERMISSION.get(report_key)


def membership_allows(membership: MembershipGrantScope, permission: str) -> bool:
    """Evaluate one membership without borrowing another membership's grants."""
    if membership.is_legacy_fallback:
        return has_permission_code({membership.role}, permission)
    return _code_allowed(set(membership.grants), set(), permission)


def compatible_membership_scopes(
    *,
    roles: Iterable[str],
    report_key: str,
    report_permission: str,
    account_kinds: Iterable[str] | None = None,
) -> tuple[MembershipGrantScope, ...]:
    """Exact boundary intersections for reports + source-domain grants.

    A ``reports:write`` grant in Branch A and ``finance:read`` in Branch B are
    deliberately not composable. Two assignments at the same boundary remain
    additive, matching the effective-permission bootstrap contract. A
    branch-wide grant intersected with a department grant yields only that
    department; an organization-wide grant intersected with a local grant
    yields only the local boundary.
    """
    if not isinstance(roles, PermissionRoleSet):
        return ()
    source_permission = domain_permission(report_key)
    if source_permission is None:
        return ()
    report_memberships = tuple(
        membership
        for membership in roles.membership_scopes
        if membership_allows(membership, report_permission)
    )
    source_memberships = tuple(
        membership
        for membership in roles.membership_scopes
        if membership_allows(membership, source_permission)
    )
    allowed_kinds = set(account_kinds) if account_kinds is not None else {"staff", "teacher"}
    intersections: dict[tuple[bool, int, int | None, str], MembershipGrantScope] = {}
    for report_membership in report_memberships:
        for source_membership in source_memberships:
            if report_membership.account_kind not in {"staff", "teacher"} or (
                source_membership.account_kind not in {"staff", "teacher"}
            ):
                continue
            boundary = _intersect_boundaries(report_membership, source_membership)
            if boundary is None:
                continue
            organization_wide, branch_id, department_id = boundary
            account_kind = (
                "teacher"
                if "teacher" in {report_membership.account_kind, source_membership.account_kind}
                else "staff"
            )
            if account_kind not in allowed_kinds:
                continue
            key = (organization_wide, branch_id, department_id, account_kind)
            intersections[key] = MembershipGrantScope(
                branch_id=branch_id,
                department_id=department_id,
                role=(
                    report_membership.role
                    if report_membership.role == source_membership.role
                    else Role.SUPPORT
                ),
                account_kind=account_kind,
                grants=frozenset(report_membership.grants | source_membership.grants),
                is_legacy_fallback=False,
                is_organization_wide=organization_wide,
            )
    matches = tuple(intersections.values())
    if report_key in ORGANIZATION_ONLY_REPORTS:
        return tuple(membership for membership in matches if membership.is_organization_wide)
    return matches


def _intersect_boundaries(
    left: MembershipGrantScope, right: MembershipGrantScope
) -> tuple[bool, int, int | None] | None:
    if left.is_organization_wide and right.is_organization_wide:
        return True, left.branch_id, None
    if left.is_organization_wide:
        return False, right.branch_id, right.department_id
    if right.is_organization_wide:
        return False, left.branch_id, left.department_id
    if left.branch_id != right.branch_id:
        return None
    if left.department_id is None:
        return False, right.branch_id, right.department_id
    if right.department_id is None:
        return False, left.branch_id, left.department_id
    if left.department_id != right.department_id:
        return None
    return False, left.branch_id, left.department_id


def can_access_report(
    *,
    report: Report,
    roles: Iterable[str],
    report_permission: str,
    is_superuser: bool = False,
) -> bool:
    """Whether the caller can read/run one library entry.

    Runtime canonical roles are grant-driven. ``allowed_roles`` remains a
    compatibility catalogue only for callers passing an old plain role set.
    """
    if is_superuser:
        return domain_permission(report.key) is not None
    if isinstance(roles, PermissionRoleSet):
        return bool(
            compatible_membership_scopes(
                roles=roles,
                report_key=report.key,
                report_permission=report_permission,
            )
        )

    source_permission = domain_permission(report.key)
    role_set = set(roles)
    if source_permission is None or not (role_set & set(report.allowed_roles or [])):
        return False
    if report.key in ORGANIZATION_ONLY_REPORTS and Role.DIRECTOR not in role_set:
        return False
    return has_permission_code(role_set, report_permission) and has_permission_code(
        role_set, source_permission
    )


def has_organization_scope(
    *, roles: Iterable[str], report_key: str, report_permission: str, is_superuser: bool = False
) -> bool:
    """True only when the exact compound grant is organization-wide."""
    if is_superuser:
        return True
    if isinstance(roles, PermissionRoleSet):
        return any(
            membership.is_organization_wide
            for membership in compatible_membership_scopes(
                roles=roles,
                report_key=report_key,
                report_permission=report_permission,
            )
        )
    # Legacy direct-selector compatibility. Never used for an HTTP/worker role
    # set, which is always PermissionRoleSet.
    return Role.DIRECTOR in set(roles)


def department_key(branch_id: int, department_id: int) -> str:
    return f"{branch_id}:{department_id}"


def teacher_key(user_id: int, branch_id: int, department_id: int | None) -> str:
    return f"{user_id}:{branch_id}:{department_id or 0}"


@dataclass(frozen=True)
class ReportScopeSnapshot:
    """Stable server-owned scope persisted inside run/schedule params."""

    organization: bool = False
    branch_ids: tuple[int, ...] = ()
    department_keys: tuple[str, ...] = ()
    teacher_keys: tuple[str, ...] = ()
    teacher_cohort_ids: tuple[int, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.organization
            or self.branch_ids
            or self.department_keys
            or self.teacher_keys
            or self.teacher_cohort_ids
        )

    def as_params(self) -> dict[str, object]:
        return {
            SCOPE_VERSION_PARAM: SCOPE_VERSION,
            SCOPE_ORGANIZATION_PARAM: self.organization,
            SCOPE_BRANCH_IDS_PARAM: list(self.branch_ids),
            SCOPE_DEPARTMENT_KEYS_PARAM: list(self.department_keys),
            SCOPE_TEACHER_KEYS_PARAM: list(self.teacher_keys),
            SCOPE_TEACHER_COHORT_IDS_PARAM: list(self.teacher_cohort_ids),
        }


def _strict_positive_ints(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, list) or len(value) > MAX_SCOPE_ITEMS:
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value):
        return None
    if value != sorted(set(value)):
        return None
    return tuple(value)


def _strict_keys(value: object, *, parts: int, allow_zero_last: bool = False) -> tuple[str, ...] | None:
    if (
        not isinstance(value, list)
        or len(value) > MAX_SCOPE_ITEMS
        or any(not isinstance(item, str) for item in value)
    ):
        return None
    if value != sorted(set(value)):
        return None
    for item in value:
        components = item.split(":")
        if len(components) != parts:
            return None
        if any(not component.isdecimal() or int(component) < 0 for component in components):
            return None
        required_positive = components[:-1] if allow_zero_last else components
        if any(int(component) < 1 for component in required_positive):
            return None
    return tuple(value)


def snapshot_from_params(params: object) -> ReportScopeSnapshot | None:
    """Parse only a complete current snapshot; malformed/legacy data fails closed."""
    if not isinstance(params, dict) or params.get(SCOPE_VERSION_PARAM) != SCOPE_VERSION:
        return None
    organization = params.get(SCOPE_ORGANIZATION_PARAM)
    if not isinstance(organization, bool):
        return None
    branch_ids = _strict_positive_ints(params.get(SCOPE_BRANCH_IDS_PARAM))
    department_keys = _strict_keys(params.get(SCOPE_DEPARTMENT_KEYS_PARAM), parts=2)
    teacher_keys = _strict_keys(params.get(SCOPE_TEACHER_KEYS_PARAM), parts=3, allow_zero_last=True)
    teacher_cohort_ids = _strict_positive_ints(params.get(SCOPE_TEACHER_COHORT_IDS_PARAM))
    if branch_ids is None or department_keys is None or teacher_keys is None or teacher_cohort_ids is None:
        return None
    snapshot = ReportScopeSnapshot(
        organization=organization,
        branch_ids=branch_ids,
        department_keys=department_keys,
        teacher_keys=teacher_keys,
        teacher_cohort_ids=teacher_cohort_ids,
    )
    if snapshot.is_empty:
        return None
    if organization and (branch_ids or department_keys or teacher_keys or teacher_cohort_ids):
        return None
    if bool(teacher_keys) != bool(teacher_cohort_ids):
        return None
    return snapshot


def public_params(params: object) -> dict[str, object]:
    """Strip every server-owned scope value before validating/displaying params."""
    if not isinstance(params, dict):
        return {}
    return {key: value for key, value in params.items() if key not in SCOPE_PARAMS}
