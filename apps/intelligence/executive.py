"""Validation, exact authorization scope, and caching identity for the snapshot."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, timedelta

from django.http import HttpRequest
from django.utils import timezone

from apps.intelligence.cache import intelligence_cache_key
from apps.intelligence.dto import (
    ExecutiveScopeBoundary,
    ExecutiveScopeLabel,
    ExecutiveSummaryScope,
    ExecutiveSummaryWindow,
)
from apps.org.models import Branch, Department
from core.exceptions import PermissionException, ValidationException
from core.permissions import (
    PermissionRoleSet,
    _request_overrides,
    get_user_roles,
    has_permission_code,
)
from core.role_principals import RolePrincipal
from core.scoping import is_permission_unscoped

EXECUTIVE_CACHE_SECONDS = 300
EXECUTIVE_MAX_WINDOW_DAYS = 366
EXECUTIVE_SECTION_REQUIREMENTS: dict[str, tuple[tuple[str, ...], ...]] = {
    # Each inner tuple is an all-of permission set; outer tuples are
    # alternatives. Every named permission must cover every selected boundary.
    "students": (("students:read",),),
    "attendance": (("attendance:read",),),
    "retention": (("students:read",),),
    "capacity": (("students:read", "cohorts:read"),),
    "risk": (("students:read", "attendance:read", "academics:read"),),
    "teachers": (("teachers:read", "attendance:read", "schedule:read"),),
    "finance": (("finance:read",),),
    # A pending request is actionable only when read authority and one handler
    # authority coexist over the selected boundary.
    "approvals": (
        ("approvals:read", "approvals:approve"),
        ("approvals:read", "approvals:disburse"),
    ),
    "tasks": (("tasks:read",),),
    # Notification feeds and meeting invitations are own-principal reads. The
    # selectors still omit them where the selected organization scope cannot be
    # represented by their models.
    "notifications": ((),),
    "meetings": ((),),
}
# Internal privacy capability used only to hide salary approvals. It is not a
# response section and therefore never appears in coverage/warnings.
_COMPENSATION_REQUIREMENTS = tuple(
    (permission,)
    for permission in (
        "compensation:read",
        "compensation:run",
        "compensation:approve",
        "compensation:disburse",
    )
)
_ALLOWED_QUERY_PARAMS = frozenset({"branch", "department", "date_from", "date_to"})
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def parse_executive_query(
    request: HttpRequest,
) -> tuple[int | None, int | None, ExecutiveSummaryWindow]:
    """Parse a deliberately closed query contract.

    Unknown, duplicate, blank, malformed, reversed, and excessively broad
    filters fail with field-scoped 400s.  The default is the inclusive trailing
    30-day window in the organization/server IANA timezone.
    """

    unknown = sorted(set(request.GET.keys()) - _ALLOWED_QUERY_PARAMS)
    if unknown:
        raise ValidationException(
            "Unknown query parameter.",
            code="unknown_query_param",
            fields={name: ["This query parameter is not supported."] for name in unknown},
        )
    for name in _ALLOWED_QUERY_PARAMS:
        if len(request.GET.getlist(name)) > 1:
            raise _filter_error(name, "Specify this query parameter only once.")

    branch_id = _positive_id(request, "branch")
    department_id = _positive_id(request, "department")
    supplied_from = _date_value(request, "date_from")
    supplied_to = _date_value(request, "date_to")
    today = timezone.localdate()
    if supplied_from is None and supplied_to is None:
        date_to = today
        date_from = date_to - timedelta(days=29)
    elif supplied_from is None:
        # The first branch already handled the both-missing case.
        date_to = supplied_to or today
        date_from = date_to - timedelta(days=29)
    else:
        date_from = supplied_from
        date_to = supplied_to or today

    if date_from > date_to:
        raise _filter_error("date_to", "Must be on or after date_from.")
    if date_to == date.max:
        # Selector bounds use an exclusive next-day timestamp.  Reject the one
        # calendar value that cannot be advanced instead of allowing an overflow.
        raise _filter_error("date_to", "Choose a date before 9999-12-31.")
    if (date_to - date_from).days + 1 > EXECUTIVE_MAX_WINDOW_DAYS:
        raise _filter_error(
            "date_to",
            f"The inclusive window may not exceed {EXECUTIVE_MAX_WINDOW_DAYS} days.",
        )
    return (
        branch_id,
        department_id,
        ExecutiveSummaryWindow(
            date_from=date_from,
            date_to=date_to,
            timezone=timezone.get_current_timezone_name(),
        ),
    )


def resolve_executive_scope(
    request: HttpRequest,
    *,
    branch_id: int | None,
    department_id: int | None,
) -> ExecutiveSummaryScope:
    """Intersect requested filters with exact intelligence-grant memberships."""

    roles = get_user_roles(request)
    organization_authority = is_permission_unscoped(
        request,
        permission="intelligence:read",
        account_kinds={"staff"},
    )
    active_branches = Branch.objects.filter(is_active=True, archived_at__isnull=True)
    if organization_authority:
        authorized = tuple(
            ExecutiveScopeBoundary(branch_id=pk)
            for pk in active_branches.order_by("pk").values_list("pk", flat=True)
        )
    else:
        authorized = _permission_boundaries(request, "intelligence:read", roles=roles)
    if not authorized:
        raise PermissionException(
            "No active management scope is available.",
            code="no_authorized_scope",
        )

    resolved_department: Department | None = None
    if department_id is not None:
        resolved_department = (
            Department.objects.select_related("branch")
            .filter(
                pk=department_id,
                is_active=True,
                branch__is_active=True,
                branch__archived_at__isnull=True,
            )
            .first()
        )
        if resolved_department is None:
            raise _scope_filter_error("department")
        if branch_id is not None and resolved_department.branch_id != branch_id:
            raise _scope_filter_error("department")
        branch_id = resolved_department.branch_id

    if branch_id is not None:
        branch_authority = tuple(boundary for boundary in authorized if boundary.branch_id == branch_id)
        if not branch_authority:
            raise _scope_filter_error("branch")
    else:
        branch_authority = authorized

    selected: tuple[ExecutiveScopeBoundary, ...]
    if resolved_department is not None:
        if not _boundaries_cover(
            branch_authority,
            ExecutiveScopeBoundary(resolved_department.branch_id, resolved_department.pk),
        ):
            raise _scope_filter_error("department")
        selected = (ExecutiveScopeBoundary(resolved_department.branch_id, resolved_department.pk),)
    else:
        selected = _normalize_boundaries(branch_authority)

    # Drop stale memberships pointing to archived/inactive organization rows.
    selected_branch_ids = {boundary.branch_id for boundary in selected}
    branches = tuple(
        ExecutiveScopeLabel(pk, name)
        for pk, name in active_branches.filter(pk__in=selected_branch_ids)
        .order_by("name", "pk")
        .values_list("pk", "name")
    )
    active_branch_ids = {branch.id for branch in branches}
    if branch_id is not None and branch_id not in active_branch_ids:
        raise _scope_filter_error("branch")
    selected = tuple(boundary for boundary in selected if boundary.branch_id in active_branch_ids)

    branch_wide_ids = {boundary.branch_id for boundary in selected if boundary.department_id is None}
    explicit_department_ids: set[int] = {
        int(boundary.department_id) for boundary in selected if boundary.department_id is not None
    }
    department_query = Department.objects.filter(is_active=True, branch_id__in=active_branch_ids)
    department_query = department_query.filter(
        # Branch-wide scopes publish every active department; restricted scopes
        # publish only the exact membership/requested boundary.
        _department_visibility_q(branch_wide_ids, explicit_department_ids)
    )
    department_rows = list(
        department_query.order_by("branch__name", "name", "pk").values_list("pk", "name", "branch_id")
    )
    valid_department_ids = {pk for pk, _name, _branch_id in department_rows}
    invalid_explicit = explicit_department_ids - valid_department_ids
    if department_id is not None and department_id in invalid_explicit:
        raise _scope_filter_error("department")
    selected = tuple(
        boundary
        for boundary in selected
        if boundary.department_id is None or boundary.department_id in valid_department_ids
    )
    if not selected:
        raise PermissionException(
            "No active management scope is available.",
            code="no_authorized_scope",
        )

    departments = tuple(ExecutiveScopeLabel(pk, name, branch_id) for pk, name, branch_id in department_rows)
    return ExecutiveSummaryScope(
        boundaries=selected,
        branches=branches,
        departments=departments,
        organization_wide=organization_authority and branch_id is None and department_id is None,
        requested_branch_id=branch_id,
        requested_department_id=department_id,
    )


def included_executive_sections(
    request: HttpRequest,
    scope: ExecutiveSummaryScope,
) -> frozenset[str]:
    """Sections whose permission covers every selected boundary."""

    roles = get_user_roles(request)
    included: set[str] = set()
    requirements = {
        **EXECUTIVE_SECTION_REQUIREMENTS,
        "_compensation": _COMPENSATION_REQUIREMENTS,
    }
    for section, alternatives in requirements.items():
        if any(
            all(
                _permission_covers_scope(
                    request,
                    permission=permission,
                    scope=scope,
                    roles=roles,
                )
                for permission in all_of
            )
            for all_of in alternatives
        ):
            included.add(section)
    return frozenset(included)


def _permission_covers_scope(
    request: HttpRequest,
    *,
    permission: str,
    scope: ExecutiveSummaryScope,
    roles: PermissionRoleSet,
) -> bool:
    if is_permission_unscoped(
        request,
        permission=permission,
        account_kinds={"staff"},
    ):
        return True
    grants = _permission_boundaries(request, permission, roles=roles)
    return bool(grants) and all(_boundaries_cover(grants, target) for target in scope.boundaries)


def _permission_boundaries(
    request: HttpRequest,
    permission: str,
    *,
    roles: PermissionRoleSet | None = None,
) -> tuple[ExecutiveScopeBoundary, ...]:
    """Exact staff memberships that individually grant ``permission``.

    Legacy assignments need the request's live tenant overrides, while canonical
    account types carry their own grant set.  Evaluating each membership in
    isolation prevents a permission from Branch A borrowing scope from Branch B.
    """

    if roles is None:
        roles = get_user_roles(request)
    overrides = _request_overrides(request) if roles.fallback_roles else {}
    boundaries: list[ExecutiveScopeBoundary] = []
    for membership in roles.membership_scopes:
        if membership.account_kind != "staff":
            continue
        if membership.is_legacy_fallback:
            allowed = has_permission_code({membership.role}, permission, overrides)
        else:
            isolated = PermissionRoleSet(canonical_grants=membership.grants)
            allowed = has_permission_code(isolated, permission)
        if allowed:
            boundaries.append(ExecutiveScopeBoundary(membership.branch_id, membership.department_id))
    return _normalize_boundaries(boundaries)


def executive_cache_key(
    request: HttpRequest,
    *,
    scope: ExecutiveSummaryScope,
    window: ExecutiveSummaryWindow,
    included_sections: frozenset[str],
    locale: str,
    currency: str,
    user_id: int,
    principal_kind: str,
    principal_id: int,
) -> str:
    return intelligence_cache_key(
        request,
        namespace="executive-summary",
        principal=RolePrincipal(
            user_id=user_id,
            kind=principal_kind,
            principal_id=principal_id,
        ),
        scope={
            "boundaries": [[boundary.branch_id, boundary.department_id] for boundary in scope.boundaries],
            "organization_wide": scope.organization_wide,
            "requested_branch": scope.requested_branch_id,
            "requested_department": scope.requested_department_id,
        },
        query={
            "sections": sorted(included_sections),
            "date_from": window.date_from.isoformat(),
            "date_to": window.date_to.isoformat(),
            "timezone": window.timezone,
            "locale": locale,
            "currency": currency,
        },
    )


def _positive_id(request: HttpRequest, name: str) -> int | None:
    if name not in request.GET:
        return None
    raw = request.GET.get(name)
    if raw is None or not raw or raw.strip() != raw:
        raise _filter_error(name, "Must be a positive integer.")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _filter_error(name, "Must be a positive integer.") from None
    if value < 1:
        raise _filter_error(name, "Must be a positive integer.")
    return value


def _date_value(request: HttpRequest, name: str) -> date | None:
    if name not in request.GET:
        return None
    raw = request.GET.get(name)
    if raw is None or _ISO_DATE_RE.fullmatch(raw) is None:
        raise _filter_error(name, "Use YYYY-MM-DD.")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise _filter_error(name, "Use a valid calendar date in YYYY-MM-DD form.") from None


def _normalize_boundaries(
    boundaries: Iterable[ExecutiveScopeBoundary],
) -> tuple[ExecutiveScopeBoundary, ...]:
    by_branch: dict[int, set[int | None]] = {}
    for boundary in boundaries:
        by_branch.setdefault(boundary.branch_id, set()).add(boundary.department_id)
    normalized: list[ExecutiveScopeBoundary] = []
    for branch_id in sorted(by_branch):
        departments = by_branch[branch_id]
        if None in departments:
            normalized.append(ExecutiveScopeBoundary(branch_id))
        else:
            normalized.extend(
                ExecutiveScopeBoundary(branch_id, department_id)
                for department_id in sorted(
                    department_id for department_id in departments if department_id is not None
                )
            )
    return tuple(normalized)


def _boundaries_cover(
    grants: tuple[ExecutiveScopeBoundary, ...],
    target: ExecutiveScopeBoundary,
) -> bool:
    return any(
        grant.branch_id == target.branch_id
        and (
            grant.department_id is None
            or (target.department_id is not None and grant.department_id == target.department_id)
        )
        for grant in grants
    )


def _department_visibility_q(branch_wide_ids: set[int], department_ids: set[int]):
    from django.db.models import Q

    query = Q(pk__in=department_ids)
    if branch_wide_ids:
        query |= Q(branch_id__in=branch_wide_ids)
    return query


def _scope_filter_error(field: str) -> ValidationException:
    return ValidationException(
        "Invalid management scope.",
        code="invalid_scope",
        fields={field: ["Choose an active scope you can access."]},
    )


def _filter_error(field: str, message: str) -> ValidationException:
    return ValidationException(
        "Invalid query parameter.",
        code="validation_error",
        fields={field: [message]},
    )
