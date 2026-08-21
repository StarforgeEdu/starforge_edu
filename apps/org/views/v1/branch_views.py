"""Branch endpoints with exact permission-bearing membership scope."""

from __future__ import annotations

import json
from datetime import date, time
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
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
from core.http import (
    bool_field,
    int_field,
    read_json,
    reject_unknown_fields,
    trimmed_str_field,
)
from core.listing import apply_filters, paginate
from core.responses import created, error, no_content, paginated, success
from core.scoping import (
    assert_permission_membership_scope,
    assert_permission_organization_scope,
    request_permission_membership_allows,
    scope_to_permission_memberships,
)
from core.validators import validate_iana_timezone

_RESOURCE = "org"
_FILTERS = ("is_active",)
_SEARCH = ("name", "slug", "address")
_ORDERING = ("name", "created_at")
_CREATE_FIELDS = frozenset(
    {
        "name",
        "slug",
        "address",
        "phone",
        "timezone",
        "is_active",
        "max_students",
        "max_teachers",
    }
)
_UPDATE_FIELDS = _CREATE_FIELDS
_HOLIDAY_FIELDS = frozenset({"date", "name", "is_working_day_override"})
_WORKING_HOUR_FIELDS = frozenset({"weekday", "opens_at", "closes_at", "is_closed"})


def _service() -> IBranchService:
    return container.resolve(IBranchService)  # type: ignore[type-abstract]


def _query(request: HttpRequest, permission: str):
    return scope_to_permission_memberships(
        request,
        _service().list(),
        permission=permission,
        branch_field="id",
    )


def _get_or_404(
    request: HttpRequest,
    branch_id: int,
    *,
    permission: str,
    for_update: bool = False,
):
    queryset = _query(request, permission)
    if for_update:
        queryset = queryset.select_for_update(of=("self",))
    branch = queryset.filter(pk=branch_id).first()
    if branch is None:
        raise NotFoundException(code="not_found")
    return branch


@csrf_exempt
@require_auth
def branches_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        permission = f"{_RESOURCE}:read"
        check_perm(request, permission)
        qs = apply_filters(
            request,
            _query(request, permission),
            filter_fields=_FILTERS,
            search_fields=_SEARCH,
            ordering_fields=_ORDERING,
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([branch_to_dict(b) for b in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        permission = f"{_RESOURCE}:write"
        check_perm(request, permission)
        # A scoped grant can manage an existing branch but cannot mint a new
        # scope that was never delegated to it.
        assert_permission_organization_scope(request, permission=permission)
        body = read_json(request)
        reject_unknown_fields(body, allowed=_CREATE_FIELDS)
        name = trimmed_str_field(body, "name", required=True, max_length=200)
        slug = trimmed_str_field(body, "slug", required=True, max_length=100)
        require_present({"name": name, "slug": slug})
        require_slug("slug", slug)
        dto = BranchCreateDTO(
            name=name,
            slug=slug,
            address=trimmed_str_field(body, "address", max_length=512),
            phone=trimmed_str_field(body, "phone", max_length=32),
            timezone=_timezone(body, "timezone", default="Asia/Tashkent"),
            is_active=bool_field(body, "is_active", default=True),
            max_students=int_field(body, "max_students", min_value=0, max_value=2_147_483_647),
            max_teachers=int_field(body, "max_teachers", min_value=0, max_value=2_147_483_647),
        )
        return created(branch_to_dict(_service().create(dto)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def branch_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    permission = f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write"
    check_perm(request, permission)
    if read:
        branch = _get_or_404(request, pk, permission=permission)
        include_capacity = request_permission_membership_allows(
            request,
            permission=permission,
            branch_id=branch.pk,
            department_id=None,
            enforce_department=True,
        )
        return success(branch_detail_to_dict(branch, include_capacity=include_capacity))
    if request.method in ("PUT", "PATCH", "DELETE"):
        with transaction.atomic():
            branch = _get_or_404(request, pk, permission=permission, for_update=True)
            # A branch has no department dimension. A department-limited grant
            # may discover its branch but must not mutate shared branch policy.
            assert_permission_membership_scope(
                request,
                permission=permission,
                branch_id=branch.pk,
                department_id=None,
                enforce_department=True,
            )
            if request.method in ("PUT", "PATCH"):
                body = read_json(request)
                reject_unknown_fields(body, allowed=_UPDATE_FIELDS)
                return success(branch_to_dict(_service().update(branch, _branch_changes(body))))
            _service().archive(branch)  # soft delete; 409 if it still has active students
            return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def branch_working_hours_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "PUT":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    permission = f"{_RESOURCE}:write"
    check_perm(request, permission)
    with transaction.atomic():
        branch = _get_or_404(request, pk, permission=permission, for_update=True)
        assert_permission_membership_scope(
            request,
            permission=permission,
            branch_id=branch.pk,
            department_id=None,
            enforce_department=True,
        )
        rows = _parse_working_hours(request)
        saved = _service().replace_working_hours(branch, rows)
        return success([working_hour_to_dict(w) for w in saved])


@csrf_exempt
@require_auth
def branch_holidays_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method == "GET":
        permission = f"{_RESOURCE}:read"
        check_perm(request, permission)
        branch = _get_or_404(request, pk, permission=permission)
        return success([holiday_to_dict(h) for h in _service().list_holidays(branch)])
    if request.method == "POST":
        permission = f"{_RESOURCE}:write"
        check_perm(request, permission)
        with transaction.atomic():
            branch = _get_or_404(request, pk, permission=permission, for_update=True)
            assert_permission_membership_scope(
                request,
                permission=permission,
                branch_id=branch.pk,
                department_id=None,
                enforce_department=True,
            )
            body = read_json(request)
            reject_unknown_fields(body, allowed=_HOLIDAY_FIELDS)
            name = trimmed_str_field(body, "name", required=True, max_length=200)
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
    permission = f"{_RESOURCE}:write"
    check_perm(request, permission)
    with transaction.atomic():
        branch = _get_or_404(request, pk, permission=permission, for_update=True)
        assert_permission_membership_scope(
            request,
            permission=permission,
            branch_id=branch.pk,
            department_id=None,
            enforce_department=True,
        )
        _service().delete_holiday(branch, holiday_id)
        return no_content()


# --- helpers ---------------------------------------------------------------
def _branch_changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "name" in body:
        changes["name"] = trimmed_str_field(body, "name", required=True, max_length=200)
        require_present({"name": changes["name"]})
    if "slug" in body:
        changes["slug"] = trimmed_str_field(body, "slug", required=True, max_length=100)
        require_present({"slug": changes["slug"]})
        require_slug("slug", changes["slug"])
    for f, max_length in (("address", 512), ("phone", 32), ("timezone", 64)):
        if f in body:
            changes[f] = (
                _timezone(body, f, default="")
                if f == "timezone"
                else trimmed_str_field(body, f, max_length=max_length)
            )
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active", default=True)
    for f in ("max_students", "max_teachers"):
        if f in body:
            changes[f] = int_field(body, f, min_value=0, max_value=2_147_483_647)
    return changes


def _parse_working_hours(request: HttpRequest) -> list[WorkingHourDTO]:
    try:
        data = json.loads(request.body or b"[]")
    except (json.JSONDecodeError, ValueError):
        raise ValidationException("Body must be valid JSON.", code="invalid_json") from None
    if not isinstance(data, list):
        raise ValidationException("Body must be a list of working-hour rows.", code="validation_error")
    if len(data) > 7:
        raise ValidationException(
            "At most seven working-hour rows are allowed.",
            code="invalid_working_hours",
            fields={"working_hours": ["At most seven rows are allowed."]},
        )
    rows: list[WorkingHourDTO] = []
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValidationException(f"Row {i} must be an object.", code="validation_error")
        reject_unknown_fields(row, allowed=_WORKING_HOUR_FIELDS)
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


def _timezone(body: dict[str, Any], name: str, *, default: str) -> str:
    value = trimmed_str_field(body, name, default=default, max_length=64)
    try:
        validate_iana_timezone(value)
    except DjangoValidationError as exc:
        message = exc.messages[0] if exc.messages else "Enter a valid IANA timezone name."
        raise ValidationException(
            "Invalid timezone.",
            code="validation_error",
            fields={name: [message]},
        ) from exc
    return value
