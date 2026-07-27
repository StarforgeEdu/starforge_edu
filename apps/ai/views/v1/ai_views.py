"""AI HTTP views (layered, off DRF).

- GET   /api/v1/ai/requests/          ai:read   — paginated request log
- GET   /api/v1/ai/requests/<id>/     ai:read
- GET   /api/v1/ai/budget/            ai:read   — current budget snapshot
- PATCH /api/v1/ai/budget/            ai:manage — update limits / is_enabled
- POST  /api/v1/ai/exam-generation/   ai:write  — 202 {request_id}
- GET   /api/v1/ai/usage-report/      ai:read   — per-feature totals
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.ai.interfaces.services import IAIService
from apps.ai.presenters import ai_request_to_dict, budget_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import read_json
from core.listing import apply_filters, paginate
from core.permissions import _request_overrides, get_user_roles, has_permission_code
from core.ratelimit import check_rate
from core.responses import error, paginated, success
from core.utils import current_schema
from core.viewsets import assert_tenant_context


def _service() -> IAIService:
    return container.resolve(IAIService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


# --- value validators (never-500) ------------------------------------------


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require(data: dict[str, Any], name: str) -> Any:
    if name not in data or data[name] is None:
        raise _reject(name, "This field is required.")
    return data[name]


def _str_value(raw: Any, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    value = raw.strip()
    if not value:
        raise _reject(name, "This field may not be blank.")
    if max_length is not None and len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _int_value(raw: Any, name: str, *, min_value: int | None = None, max_value: int | None = None) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise _reject(name, "A valid integer is required.")
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise _reject(name, "A valid integer is required.") from None
    if min_value is not None and value < min_value:
        raise _reject(name, f"Ensure this value is greater than or equal to {min_value}.")
    if max_value is not None and value > max_value:
        raise _reject(name, f"Ensure this value is less than or equal to {max_value}.")
    return value


# Mirror DRF BooleanField's TRUE_VALUES/FALSE_VALUES (lowercased) so is_enabled parity
# holds: "on"/"y" -> True, a garbage/typo string -> 400 (NOT a silent coerce to False,
# which would disable AI center-wide on a malformed value).
_TRUE_VALUES = frozenset({"true", "1", "yes", "y", "t", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "n", "f", "off"})


def _bool_value(raw: Any, name: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in _TRUE_VALUES:
            return True
        if value in _FALSE_VALUES:
            return False
    raise _reject(name, "Must be a valid boolean.")


def _choice_value(raw: Any, name: str, choices) -> str:
    if not isinstance(raw, str) or raw not in choices:
        raise _reject(name, f"Must be one of: {', '.join(choices)}.")
    return raw


def _query_datetime(raw: str, name: str):
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        # Accept a date-only value too (the old DateTimeFilter used Django's
        # DATETIME_INPUT_FORMATS, which include "%Y-%m-%d" -> that date at midnight).
        try:
            day = parse_date(raw)
        except ValueError:
            day = None
        if day is not None:
            parsed = datetime(day.year, day.month, day.day)
    if parsed is None:
        raise ValidationException(
            "Invalid query parameter.",
            code="invalid_query_param",
            fields={name: ["Enter a valid ISO 8601 date or datetime."]},
        )
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _month_bounds(month: str | None) -> tuple[date, date]:
    """Parse YYYY-MM (default: current month) into inclusive day bounds."""
    if month:
        try:
            anchor = datetime.strptime(month, "%Y-%m").date()
        except (ValueError, TypeError) as exc:
            raise ValidationException("month must be formatted as YYYY-MM.", code="invalid_month") from exc
    else:
        anchor = timezone.localdate()
    try:
        start = anchor.replace(day=1)
        # December of year 9999 rolls to year 10000, which date.replace rejects with a
        # ValueError — a valid-format-but-unpageable month must be a 400, not a 500.
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1)
        else:
            next_month = start.replace(month=start.month + 1)
    except (ValueError, OverflowError) as exc:
        raise ValidationException("month is out of range.", code="invalid_month") from exc
    return start, next_month - timedelta(days=1)


# --- request log -----------------------------------------------------------


@csrf_exempt
@require_auth
def ai_requests_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "ai:read")
    assert_tenant_context()
    qs = _service().list_requests()
    created_after = request.GET.get("created_after")
    created_before = request.GET.get("created_before")
    if created_after:
        qs = qs.filter(created_at__gte=_query_datetime(created_after, "created_after"))
    if created_before:
        qs = qs.filter(created_at__lte=_query_datetime(created_before, "created_before"))
    qs = apply_filters(
        request,
        qs,
        filter_fields=("feature", "status"),
        ordering_fields=("created_at",),
        default_ordering="-created_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([ai_request_to_dict(r) for r in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def ai_request_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "ai:read")
    assert_tenant_context()
    req = _service().get_request(pk=pk)
    if req is None:
        raise NotFoundException(code="not_found")
    roles = get_user_roles(request)
    can_view_output = (
        req.requested_by_id == request.user.id
        or request.user.is_superuser
        or has_permission_code(roles, "ai:manage", _request_overrides(request))
    )
    return success(ai_request_to_dict(req, include_output=can_view_output))


# --- budget ----------------------------------------------------------------


@csrf_exempt
@require_auth
def budget_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "ai:read")
        return success(budget_to_dict(_service().get_budget()))
    if request.method == "PATCH":
        check_perm(request, "ai:manage")
        data = read_json(request)
        daily = (
            _int_value(data["daily_token_limit"], "daily_token_limit", min_value=0)
            if "daily_token_limit" in data
            else None
        )
        monthly = (
            _int_value(data["monthly_token_limit"], "monthly_token_limit", min_value=0)
            if "monthly_token_limit" in data
            else None
        )
        is_enabled = _bool_value(data["is_enabled"], "is_enabled") if "is_enabled" in data else None
        if daily is None and monthly is None and is_enabled is None:
            raise _reject("non_field_errors", "At least one field is required.")
        budget = _service().update_budget(
            daily_token_limit=daily, monthly_token_limit=monthly, is_enabled=is_enabled
        )
        return success(budget_to_dict(budget))
    return _method_not_allowed()


# --- exam generation -------------------------------------------------------


@csrf_exempt
@require_auth
def exam_generation_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "ai:write")
    # A per-request rate cap (20/min per schema+user) on top of the token budget —
    # stops request flooding before budget accounting runs (mirrors AIGenerationThrottle).
    check_rate(scope="ai_generation", key=f"{current_schema()}:{request.user.pk}", limit=20, window=60)
    data = read_json(request)
    subject_id = _int_value(_require(data, "subject_id"), "subject_id", min_value=1)
    exam_type = _str_value(_require(data, "exam_type"), "exam_type", max_length=32)
    question_count = _int_value(
        _require(data, "question_count"), "question_count", min_value=1, max_value=200
    )
    difficulty = _choice_value(_require(data, "difficulty"), "difficulty", ("easy", "medium", "hard"))
    ai_request = _service().request_exam_generation(
        requested_by=request.user,
        subject_id=subject_id,
        exam_type=exam_type,
        question_count=question_count,
        difficulty=difficulty,
    )
    return success({"request_id": ai_request.pk}, status=202)


# --- usage report ----------------------------------------------------------


@csrf_exempt
@require_auth
def usage_report_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "ai:read")
    start, end = _month_bounds(request.GET.get("month"))
    return success(_service().usage_report(start=start, end=end))
