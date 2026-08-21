"""Audit endpoints — plain Django views over the layered architecture (D3-D-4, D3-D-7).

Append-only + read-only by construction: the collection/detail views only answer GET
(any write verb -> 405 against the immutable model), and there is no create/update path.
All actions are gated at ``audit:read``. The list uses keyset cursor pagination
(``core.listing.cursor_paginate``) so the timeline stays stable under concurrent inserts,
and the CSV export streams the same filtered timeline (refusing a result set over
``MAX_EXPORT_ROWS`` and auditing itself as an ``export`` row).
"""

from __future__ import annotations

import csv
from datetime import datetime

from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    JsonResponse,
    StreamingHttpResponse,
)
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.audit.dto.audit_dto import AuditFilterDTO, AuditVisibilityDTO
from apps.audit.interfaces.services import IAuditService
from apps.audit.models import AuditLog
from apps.audit.openapi_contracts import (
    AUDIT_DETAIL_GET_CONTRACT,
    AUDIT_DETAIL_HEAD_CONTRACT,
    AUDIT_EXPORT_GET_CONTRACT,
    AUDIT_EXPORT_HEAD_CONTRACT,
    AUDIT_LIST_GET_CONTRACT,
    AUDIT_LIST_HEAD_CONTRACT,
)
from apps.audit.presenters import audit_to_dict
from apps.audit.scopes import (
    AuditScopeSnapshot,
    organization_audit_scope,
    scoped_audit_scope,
    unresolved_audit_scope,
)
from apps.audit.services import audit_log
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.listing import cursor_paginate
from core.openapi_contracts import openapi_contract
from core.permissions import get_user_roles
from core.responses import error, success
from core.scoping import permission_membership_scopes
from core.tenant_context import assert_tenant_context

# A CSV stream beyond this size is a misuse of the export endpoint; force the caller
# to narrow filters rather than dump the entire trail. Module-level so tests can patch it.
MAX_EXPORT_ROWS = 50_000

# Audit access is deliberately narrower than operational maker/checker grants.
# A payroll runner, approver, or cashier may perform its assigned workflow
# without gaining a historical salary/profile oracle through ``audit:read``.
_COMPENSATION_AUDIT_PERMISSION = "compensation:read"
_FILTER_QUERY_FIELDS = frozenset(
    {
        "actor",
        "actor_principal_kind",
        "actor_principal_id",
        "action",
        "resource_type",
        "resource_id",
        "ts_from",
        "ts_to",
        "branch",
        "department",
        "scope_status",
        "sensitivity",
    }
)
_COLLECTION_QUERY_FIELDS = _FILTER_QUERY_FIELDS | {"cursor", "page_size"}
_ACTOR_PRINCIPAL_KINDS = frozenset({"user", "student", "teacher", "parent", "staff"})


def _service() -> IAuditService:
    return container.resolve(IAuditService)  # type: ignore[type-abstract]


def _filters(request: HttpRequest) -> AuditFilterDTO:
    actor_principal_kind, actor_principal_id = _actor_principal_params(request)
    ts_from = _dt_param(request, "ts_from")
    ts_to = _dt_param(request, "ts_to")
    if ts_from is not None and ts_to is not None and ts_from > ts_to:
        raise ValidationException(
            "The audit time range is reversed.",
            code="validation_error",
            fields={"ts_to": ["Choose a value on or after ts_from."]},
        )
    return AuditFilterDTO(
        actor=_positive_int_param(request, "actor"),
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
        action=_action_param(request),
        resource_type=_bounded_param(request, "resource_type", max_length=100),
        resource_id=_bounded_param(request, "resource_id", max_length=64),
        ts_from=ts_from,
        ts_to=ts_to,
        branch=_positive_int_param(request, "branch"),
        department=_positive_int_param(request, "department"),
        scope_status=_scope_status_param(request),
        sensitivity=_sensitivity_param(request),
    )


def _visibility(request: HttpRequest) -> AuditVisibilityDTO:
    if request.user.is_superuser:
        return AuditVisibilityDTO(
            organization_wide=True,
            compensation_organization_wide=True,
        )

    roles = get_user_roles(request)
    memberships = permission_membership_scopes(
        roles=roles,
        permission="audit:read",
    )
    compensation_memberships = permission_membership_scopes(
        roles=roles,
        permission=_COMPENSATION_AUDIT_PERMISSION,
        account_kinds={"staff"},
    )

    return AuditVisibilityDTO(
        organization_wide=any(membership.is_organization_wide for membership in memberships),
        branch_wide_ids=frozenset(
            membership.branch_id for membership in memberships if membership.department_id is None
        ),
        department_scopes=frozenset(
            (membership.branch_id, membership.department_id)
            for membership in memberships
            if membership.department_id is not None
        ),
        compensation_organization_wide=any(
            membership.is_organization_wide for membership in compensation_memberships
        ),
        compensation_branch_wide_ids=frozenset(
            membership.branch_id
            for membership in compensation_memberships
            if not membership.is_organization_wide and membership.department_id is None
        ),
        compensation_department_scopes=frozenset(
            (membership.branch_id, membership.department_id)
            for membership in compensation_memberships
            if not membership.is_organization_wide and membership.department_id is not None
        ),
    )


@openapi_contract(
    path="/api/v1/audit/",
    operations=(AUDIT_LIST_GET_CONTRACT, AUDIT_LIST_HEAD_CONTRACT),
)
@csrf_exempt
@require_auth
def audit_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        # Immutable trail: PUT/PATCH/DELETE/POST -> 405 (no mutation path).
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "audit:read")
    assert_tenant_context()  # never serve the trail on the public schema
    _validate_query(request, allowed=_COLLECTION_QUERY_FIELDS)
    qs = _service().filtered(_filters(request), _visibility(request))
    rows, next_link, previous_link = cursor_paginate(request, qs)
    return JsonResponse(
        {"results": [audit_to_dict(r) for r in rows], "next": next_link, "previous": previous_link}
    )


@openapi_contract(
    path="/api/v1/audit/{pk}/",
    operations=(AUDIT_DETAIL_GET_CONTRACT, AUDIT_DETAIL_HEAD_CONTRACT),
)
@csrf_exempt
@require_auth
def audit_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "audit:read")
    assert_tenant_context()
    _validate_query(request, allowed=frozenset())
    row = _service().get(pk, _visibility(request))
    if row is None:
        raise NotFoundException(code="not_found")
    # Standard success envelope — matches every other <resource>_detail_view and lets the
    # availability middleware inject degraded-mode `warnings` (it keys on a top-level
    # "success"). The COLLECTION view stays a bare {results,next,previous} cursor feed.
    return success(audit_to_dict(row))


@openapi_contract(
    path="/api/v1/audit/export/",
    operations=(AUDIT_EXPORT_GET_CONTRACT, AUDIT_EXPORT_HEAD_CONTRACT),
)
@csrf_exempt
@require_auth
def audit_export_view(request: HttpRequest) -> HttpResponseBase:
    """Streaming CSV of the filtered trail (same filters as the list). A result set over
    ``MAX_EXPORT_ROWS`` is refused 400; the export is itself recorded as an audit row."""
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "audit:read")
    assert_tenant_context()
    _validate_query(request, allowed=_FILTER_QUERY_FIELDS)
    visibility = _visibility(request)
    qs = _freeze_export_queryset(_service().filtered(_filters(request), visibility))
    total = qs.count()
    if total > MAX_EXPORT_ROWS:
        raise ValidationException(
            "Too many rows to export; narrow your filters.",
            code="validation_error",
            fields={"rows": [f"{total} rows match (max {MAX_EXPORT_ROWS})."]},
        )

    if request.method == "HEAD":
        # HEAD must be observational: advertise the same media/disposition
        # without creating a phantom EXPORT audit event or opening a stream.
        head_response = HttpResponse(content_type="text/csv")
        head_response["Content-Disposition"] = 'attachment; filename="audit_log.csv"'
        return head_response

    audit_log(
        actor=request.user,
        action=AuditLog.Action.EXPORT,
        resource_type="audit.AuditLog",
        after={"rows": total, "filters": dict(request.GET)},
        request=request,
        scope=_export_scope(qs, visibility),
    )

    streaming_response = StreamingHttpResponse(_csv_rows(qs), content_type="text/csv")
    streaming_response["Content-Disposition"] = 'attachment; filename="audit_log.csv"'
    return streaming_response


# --- CSV streaming ---------------------------------------------------------
_CSV_HEADER = [
    "id",
    "created_at",
    "actor_id",
    "actor_repr",
    "actor_attribution_status",
    "actor_principal_kind",
    "actor_principal_id",
    "action",
    "resource_type",
    "resource_id",
    "scope_status",
    "scope_branch_id",
    "scope_department_id",
    "sensitivity",
    "ip",
    "user_agent",
]


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value):
    """Neutralize spreadsheet formula injection. Audit cells carry attacker-controlled
    text (User-Agent header, actor_repr, resource ids); a leading = + - @ (or tab/CR)
    would execute as a formula when an admin opens the export. Prefix such strings with
    an apostrophe so they render as literal text (mirrors reports.generators.safe_cell)."""
    if isinstance(value, str) and value[:1] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def _csv_rows(qs):
    writer = csv.writer(_Echo())
    yield writer.writerow(_CSV_HEADER)
    for row in qs.iterator():
        yield writer.writerow(
            [
                row.id,
                row.created_at.isoformat(),
                row.actor_id or "",
                _safe_cell(row.actor_repr),
                row.actor_attribution_status,
                row.actor_principal_kind,
                row.actor_principal_id or "",
                _safe_cell(row.action),
                _safe_cell(row.resource_type),
                _safe_cell(row.resource_id),
                row.scope_status,
                row.scope_branch_id or "",
                row.scope_department_id or "",
                row.sensitivity,
                row.ip or "",
                _safe_cell(row.user_agent),
            ]
        )


class _Echo:
    """Write-only file-like object that returns each row for StreamingHttpResponse."""

    def write(self, value: str) -> str:
        return value


# --- query-param parsing (bad value -> 400) --------------------------------
def _int_param(request: HttpRequest, name: str) -> int | None:
    raw = request.GET.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationException(
            f"Query parameter '{name}' must be an integer.",
            code="validation_error",
            fields={name: ["Enter a valid integer."]},
        ) from exc


def _positive_int_param(request: HttpRequest, name: str) -> int | None:
    value = _int_param(request, name)
    if value is not None and value < 1:
        raise ValidationException(
            f"Query parameter '{name}' must be a positive integer.",
            code="validation_error",
            fields={name: ["Enter a positive integer."]},
        )
    return value


def _validate_query(request: HttpRequest, *, allowed: frozenset[str]) -> None:
    unknown = sorted(set(request.GET) - allowed)
    duplicate = sorted(name for name, values in request.GET.lists() if len(values) != 1)
    fields: dict[str, list[str]] = {}
    fields.update({name: ["This query parameter is not supported."] for name in unknown})
    fields.update({name: ["Supply this parameter once."] for name in duplicate})
    if fields:
        raise ValidationException(
            "The audit filters are invalid.",
            code="validation_error",
            fields=fields,
        )


def _bounded_param(request: HttpRequest, name: str, *, max_length: int) -> str | None:
    raw = request.GET.get(name)
    if raw is None or raw == "":
        return None
    if len(raw) > max_length:
        raise ValidationException(
            f"Query parameter '{name}' is too long.",
            code="validation_error",
            fields={name: [f"Use at most {max_length} characters."]},
        )
    return raw


def _action_param(request: HttpRequest) -> str | None:
    raw = request.GET.get("action")
    if not raw:
        return None
    allowed = {choice for choice, _label in AuditLog.Action.choices}
    if raw not in allowed:
        raise ValidationException(
            "Query parameter 'action' is invalid.",
            code="validation_error",
            fields={"action": ["Choose a documented audit action."]},
        )
    return raw


def _actor_principal_params(request: HttpRequest) -> tuple[str | None, int | None]:
    kind = request.GET.get("actor_principal_kind") or None
    principal_id = _positive_int_param(request, "actor_principal_id")
    if (kind is None) != (principal_id is None):
        raise ValidationException(
            "Both actor principal filters are required together.",
            code="validation_error",
            fields={
                "actor_principal_kind": ["Supply kind and id together."],
                "actor_principal_id": ["Supply kind and id together."],
            },
        )
    if kind is not None and kind not in _ACTOR_PRINCIPAL_KINDS:
        raise ValidationException(
            "Query parameter 'actor_principal_kind' is invalid.",
            code="validation_error",
            fields={"actor_principal_kind": ["Choose a documented principal kind."]},
        )
    return kind, principal_id


def _scope_status_param(request: HttpRequest) -> str | None:
    raw = request.GET.get("scope_status")
    if not raw:
        return None
    allowed = {choice for choice, _label in AuditLog.ScopeStatus.choices}
    if raw not in allowed:
        raise ValidationException(
            "Query parameter 'scope_status' is invalid.",
            code="validation_error",
            fields={"scope_status": ["Choose scoped, organization, or unresolved."]},
        )
    return raw


def _sensitivity_param(request: HttpRequest) -> str | None:
    raw = request.GET.get("sensitivity")
    if not raw:
        return None
    allowed = {choice for choice, _label in AuditLog.Sensitivity.choices}
    if raw not in allowed:
        raise ValidationException(
            "Query parameter 'sensitivity' is invalid.",
            code="validation_error",
            fields={"sensitivity": ["Choose standard or compensation."]},
        )
    return raw


def _dt_param(request: HttpRequest, name: str) -> datetime | None:
    raw = request.GET.get(name)
    if not raw:
        return None
    try:
        # parse_datetime RAISES ValueError on a regex-valid but out-of-range value
        # (e.g. 2026-02-30T00:00) — not just returns None — so catch it: a bad query
        # param must be a clean 400, never a 500.
        parsed = parse_datetime(raw)
    except ValueError:
        parsed = None
    if parsed is None or not timezone.is_aware(parsed):
        raise ValidationException(
            f"Query parameter '{name}' must be a valid ISO 8601 datetime.",
            code="validation_error",
            fields={name: ["Enter a valid ISO 8601 datetime."]},
        )
    return parsed


def _export_scope(qs, visibility: AuditVisibilityDTO) -> AuditScopeSnapshot:
    """Attribute the export without broadening its underlying row visibility."""
    if visibility.organization_wide:
        return organization_audit_scope()

    boundaries = list(qs.order_by().values_list("scope_branch_id", "scope_department_id").distinct()[:2])
    if len(boundaries) == 1 and boundaries[0][0] is not None:
        return scoped_audit_scope(boundaries[0][0], boundaries[0][1])
    if not boundaries:
        possible = {(branch_id, None) for branch_id in visibility.branch_wide_ids} | set(
            visibility.department_scopes
        )
        if len(possible) == 1:
            branch_id, department_id = possible.pop()
            return scoped_audit_scope(branch_id, department_id)
    return unresolved_audit_scope()


def _freeze_export_queryset(qs):
    """Exclude the export's own audit row and concurrent later inserts.

    Streaming evaluates the queryset after the EXPORT event is inserted. A
    monotonic primary-key upper bound freezes the selected timeline before that
    insert so the advertised count and streamed rows describe the same set.
    """
    last_pk = qs.order_by("-pk").values_list("pk", flat=True).first()
    return qs.filter(pk__lte=last_pk) if last_pk is not None else qs.none()
