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
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.audit.dto.audit_dto import AuditFilterDTO
from apps.audit.interfaces.services import IAuditService
from apps.audit.models import AuditLog
from apps.audit.presenters import audit_to_dict
from apps.audit.services import audit_log
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.listing import cursor_paginate
from core.responses import error, success
from core.utils import client_ip, user_agent
from core.viewsets import assert_tenant_context

# A CSV stream beyond this size is a misuse of the export endpoint; force the caller
# to narrow filters rather than dump the entire trail. Module-level so tests can patch it.
MAX_EXPORT_ROWS = 50_000


def _service() -> IAuditService:
    return container.resolve(IAuditService)  # type: ignore[type-abstract]


def _filters(request: HttpRequest) -> AuditFilterDTO:
    return AuditFilterDTO(
        actor=_int_param(request, "actor"),
        action=request.GET.get("action") or None,
        resource_type=request.GET.get("resource_type") or None,
        resource_id=request.GET.get("resource_id") or None,
        ts_from=_dt_param(request, "ts_from"),
        ts_to=_dt_param(request, "ts_to"),
    )


@csrf_exempt
@require_auth
def audit_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        # Immutable trail: PUT/PATCH/DELETE/POST -> 405 (no mutation path).
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "audit:read")
    assert_tenant_context()  # never serve the trail on the public schema
    qs = _service().filtered(_filters(request))
    rows, next_link, previous_link = cursor_paginate(request, qs)
    return JsonResponse(
        {"results": [audit_to_dict(r) for r in rows], "next": next_link, "previous": previous_link}
    )


@csrf_exempt
@require_auth
def audit_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "audit:read")
    assert_tenant_context()
    row = _service().get(pk)
    if row is None:
        raise NotFoundException(code="not_found")
    # Standard success envelope — matches every other <resource>_detail_view and lets the
    # availability middleware inject degraded-mode `warnings` (it keys on a top-level
    # "success"). The COLLECTION view stays a bare {results,next,previous} cursor feed.
    return success(audit_to_dict(row))


@csrf_exempt
@require_auth
def audit_export_view(request: HttpRequest) -> HttpResponseBase:
    """Streaming CSV of the filtered trail (same filters as the list). A result set over
    ``MAX_EXPORT_ROWS`` is refused 400; the export is itself recorded as an audit row."""
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "audit:read")
    assert_tenant_context()
    qs = _service().filtered(_filters(request))
    total = qs.count()
    if total > MAX_EXPORT_ROWS:
        raise ValidationException(
            "Too many rows to export; narrow your filters.",
            code="validation_error",
            fields={"rows": [f"{total} rows match (max {MAX_EXPORT_ROWS})."]},
        )

    audit_log(
        actor=request.user,
        action=AuditLog.Action.EXPORT,
        resource_type="audit.AuditLog",
        after={"rows": total, "filters": dict(request.GET)},
        ip=client_ip(request) or None,
        user_agent=user_agent(request),
    )

    response = StreamingHttpResponse(_csv_rows(qs), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="audit_log.csv"'
    return response


# --- CSV streaming ---------------------------------------------------------
_CSV_HEADER = [
    "id",
    "created_at",
    "actor_id",
    "actor_repr",
    "action",
    "resource_type",
    "resource_id",
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
                _safe_cell(row.action),
                _safe_cell(row.resource_type),
                _safe_cell(row.resource_id),
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
    if parsed is None:
        raise ValidationException(
            f"Query parameter '{name}' must be a valid ISO 8601 datetime.",
            code="validation_error",
            fields={name: ["Enter a valid ISO 8601 datetime."]},
        )
    return parsed
