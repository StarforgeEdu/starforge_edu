"""Branch endpoints — plain Django views over the layered architecture.

Branches are NOT branch-scoped (they *are* the scope), so there is no branch
filtering here — just the org:read / org:write gate. destroy soft-deletes
(archive). The working-hours / holidays sub-resources hang off the detail route.
"""

from __future__ import annotations

import json
from datetime import date, time
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.org.dto.org_dto import BranchCreateDTO, HolidayCreateDTO, WorkingHourDTO
from apps.org.interfaces.services import IBranchService
from apps.org.presenters import (
    branch_detail_to_dict,
    branch_to_dict,
    holiday_to_dict,
    working_hour_to_dict,
)
from apps.org.views.v1._shared import require_present, require_slug
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.responses import created, error, no_content, paginated, success

_RESOURCE = "org"
_FILTERS = ("is_active",)
_SEARCH = ("name", "slug", "address")
_ORDERING = ("name", "created_at")


def _service() -> IBranchService:
    return container.resolve(IBranchService)  # type: ignore[type-abstract]


def _get_or_404(branch_id: int):
    branch = _service().get(branch_id)
    if branch is None:
        raise NotFoundException(code="not_found")
    return branch


@csrf_exempt
@require_auth
def branches_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = apply_filters(
            request,
            _service().list(),
            filter_fields=_FILTERS,
            search_fields=_SEARCH,
            ordering_fields=_ORDERING,
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([branch_to_dict(b) for b in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        name, slug = str_field(body, "name"), str_field(body, "slug")
        require_present({"name": name, "slug": slug})
        require_slug("slug", slug)
        dto = BranchCreateDTO(
            name=name,
            slug=slug,
            address=str_field(body, "address"),
            phone=str_field(body, "phone"),
            timezone=str_field(body, "timezone", default="Asia/Tashkent"),
            is_active=bool_field(body, "is_active", default=True),
            max_students=int_field(body, "max_students"),
            max_teachers=int_field(body, "max_teachers"),
        )
        return created(branch_to_dict(_service().create(dto)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def branch_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    branch = _get_or_404(pk)
    if read:
        return success(branch_detail_to_dict(branch))
    if request.method in ("PUT", "PATCH"):
        return success(branch_to_dict(_service().update(branch, _branch_changes(read_json(request)))))
    if request.method == "DELETE":
        _service().archive(branch)  # soft delete; 409 if it still has active students
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def branch_working_hours_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "PUT":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    branch = _get_or_404(pk)
    rows = _parse_working_hours(request)
    saved = _service().replace_working_hours(branch, rows)
    return success([working_hour_to_dict(w) for w in saved])


@csrf_exempt
@require_auth
def branch_holidays_view(request: HttpRequest, pk: int) -> HttpResponse:
    # The action perm is org:read (both verbs); a POST additionally needs org:write.
    check_perm(request, f"{_RESOURCE}:read")
    branch = _get_or_404(pk)
    if request.method == "GET":
        return success([holiday_to_dict(h) for h in _service().list_holidays(branch)])
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        name = str_field(body, "name")
        require_present({"name": name})
        dto = HolidayCreateDTO(
            date=_date(body, "date", required=True),  # type: ignore[arg-type]
            name=name,
            is_working_day_override=bool_field(body, "is_working_day_override"),
        )
        return created(holiday_to_dict(_service().add_holiday(branch, dto)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def branch_delete_holiday_view(request: HttpRequest, pk: int, holiday_id: int) -> HttpResponse:
    if request.method != "DELETE":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    branch = _get_or_404(pk)
    _service().delete_holiday(branch, holiday_id)
    return no_content()


# --- helpers ---------------------------------------------------------------
def _branch_changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for f in ("name", "slug", "address", "phone", "timezone"):
        if f in body:
            changes[f] = str_field(body, f)
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active", default=True)
    for f in ("max_students", "max_teachers"):
        if f in body:
            changes[f] = int_field(body, f)
    return changes


def _parse_working_hours(request: HttpRequest) -> list[WorkingHourDTO]:
    try:
        data = json.loads(request.body or b"[]")
    except (json.JSONDecodeError, ValueError):
        raise ValidationException("Body must be valid JSON.", code="invalid_json") from None
    if not isinstance(data, list):
        raise ValidationException(
            "Body must be a list of working-hour rows.", code="validation_error"
        )
    rows: list[WorkingHourDTO] = []
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValidationException(f"Row {i} must be an object.", code="validation_error")
        weekday = int_field(row, "weekday", required=True)
        if weekday is None or not (0 <= weekday <= 6):
            raise ValidationException(
                "weekday must be 0-6.", code="validation_error", fields={"weekday": ["Must be 0-6."]}
            )
        rows.append(
            WorkingHourDTO(
                weekday=weekday,
                opens_at=_time(row, "opens_at"),
                closes_at=_time(row, "closes_at"),
                is_closed=bool_field(row, "is_closed"),
            )
        )
    return rows


def _time(row: dict[str, Any], name: str) -> time:
    raw = row.get(name)
    try:
        return time.fromisoformat(str(raw))
    except (ValueError, TypeError):
        raise ValidationException(
            "Invalid time.", code="validation_error", fields={name: ["Must be HH:MM."]}
        ) from None


def _date(body: dict[str, Any], name: str, *, required: bool = False) -> date | None:
    raw = body.get(name)
    if raw in (None, ""):
        if required:
            raise ValidationException(
                "Date is required.", code="validation_error", fields={name: ["This field is required."]}
            )
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise ValidationException(
            "Invalid date.", code="validation_error", fields={name: ["Must be an ISO date."]}
        ) from None
