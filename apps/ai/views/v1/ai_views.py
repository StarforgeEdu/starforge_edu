"""AI HTTP views (layered, off DRF).

- GET   /api/v1/ai/requests/          ai:read   — paginated request log
- GET   /api/v1/ai/requests/<id>/     ai:read
- GET   /api/v1/ai/budget/            ai:manage — current organization budget
- PATCH /api/v1/ai/budget/            ai:manage — update limits / is_enabled
- POST  /api/v1/ai/exam-generation/   ai:write  — 202 {request_id}
- GET   /api/v1/ai/usage-report/      ai:manage — organization-wide totals
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.ai.interfaces.services import IAIService
from apps.ai.models import AIFeature, AIRequest
from apps.ai.openapi_contracts import (
    AI_BUDGET_CONTRACTS,
    AI_EXAM_GENERATION_CONTRACT,
    AI_REQUEST_COLLECTION_CONTRACTS,
    AI_REQUEST_DETAIL_CONTRACTS,
    AI_USAGE_REPORT_CONTRACTS,
)
from apps.ai.presenters import ai_request_to_dict, budget_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import read_json, reject_unknown_fields
from core.listing import apply_filters, paginate, validate_pagination_filters
from core.openapi_contracts import openapi_contract
from core.permissions import get_user_roles
from core.ratelimit import check_rate
from core.responses import error, paginated, success
from core.role_principals import RolePrincipal, request_role_principal
from core.scoping import (
    assert_permission_organization_scope,
    is_permission_unscoped,
    request_permission_membership_allows,
)
from core.tenant_context import assert_tenant_context
from core.utils import current_schema


def _service() -> IAIService:
    return container.resolve(IAIService)  # type: ignore[type-abstract]


def _principal(request: HttpRequest) -> RolePrincipal:
    cached = getattr(request, "_ai_role_principal", None)
    if isinstance(cached, RolePrincipal):
        return cached
    principal = request_role_principal(request, error_code="ai_principal_unavailable")
    request._ai_role_principal = principal  # type: ignore[attr-defined]
    return principal


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


def _query_datetime(raw: str, name: str) -> tuple[datetime, bool]:
    # ``parse_datetime`` also accepts ``YYYY-MM-DD`` and turns it into midnight.
    # Parse the exact date form first so an upper date bound can include the full
    # organization-local calendar day instead of silently stopping at 00:00.
    try:
        day = parse_date(raw)
    except ValueError:
        day = None
    if day is not None:
        parsed = datetime(day.year, day.month, day.day)
        date_only = True
    else:
        try:
            parsed = parse_datetime(raw)
        except ValueError:
            parsed = None
        date_only = False
    if parsed is None:
        raise ValidationException(
            "Invalid query parameter.",
            code="invalid_query_param",
            fields={name: ["Enter a valid ISO 8601 date or datetime."]},
        )
    value = timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
    return value, date_only


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


@openapi_contract(
    path="/api/v1/ai/requests/",
    operations=AI_REQUEST_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def ai_requests_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "ai:read")
    assert_tenant_context()
    principal = _principal(request)
    roles = get_user_roles(request)
    qs = _service().list_requests(
        roles=roles,
        principal=principal,
        is_superuser=bool(request.user.is_superuser),
    )
    feature = request.GET.get("feature")
    status_value = request.GET.get("status")
    if feature is not None and feature not in AIFeature.values:
        raise _reject("feature", "Choose a supported AI feature.")
    if status_value is not None and status_value not in AIRequest.Status.values:
        raise _reject("status", "Choose a supported AI request status.")
    created_after = request.GET.get("created_after")
    created_before = request.GET.get("created_before")
    after_value = None
    before_value = None
    before_is_date = False
    if created_after:
        after_value, _after_is_date = _query_datetime(created_after, "created_after")
        qs = qs.filter(created_at__gte=after_value)
    if created_before:
        before_value, before_is_date = _query_datetime(created_before, "created_before")
        if before_is_date:
            before_value += timedelta(days=1)
            qs = qs.filter(created_at__lt=before_value)
        else:
            qs = qs.filter(created_at__lte=before_value)
    if (
        after_value is not None
        and before_value is not None
        and (after_value >= before_value if before_is_date else after_value > before_value)
    ):
        raise ValidationException(
            "Invalid query range.",
            code="invalid_query_param",
            fields={"created_before": ["Must not be earlier than created_after."]},
        )
    allowed_query = {"feature", "status", "created_after", "created_before", "ordering", "page", "page_size"}
    unknown = sorted(set(request.GET) - allowed_query)
    if unknown:
        raise ValidationException(
            "Unsupported query parameter.",
            code="invalid_query_param",
            fields={name: ["This query parameter is not supported."] for name in unknown},
        )
    validate_pagination_filters(request)
    qs = apply_filters(
        request,
        qs,
        filter_fields=("feature", "status"),
        ordering_fields=("created_at",),
        default_ordering="-created_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([ai_request_to_dict(r) for r in items], total=total, page=page, page_size=size)


@openapi_contract(
    path="/api/v1/ai/requests/{pk}/",
    operations=AI_REQUEST_DETAIL_CONTRACTS,
)
@csrf_exempt
@require_auth
def ai_request_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "ai:read")
    assert_tenant_context()
    principal = _principal(request)
    roles = get_user_roles(request)
    req = _service().get_request(
        pk=pk,
        roles=roles,
        principal=principal,
        is_superuser=bool(request.user.is_superuser),
    )
    if req is None:
        raise NotFoundException(code="not_found")
    is_exact_requester = (
        req.requested_by_id == principal.user_id
        and req.requested_principal_kind == principal.kind
        and req.requested_principal_id == principal.principal_id
    )
    if req.scope_status == req.ScopeStatus.ORGANIZATION:
        has_manage_scope = is_permission_unscoped(request, permission="ai:manage")
    else:
        has_manage_scope = request_permission_membership_allows(
            request,
            permission="ai:manage",
            branch_id=req.branch_at_request_id,
            department_id=req.department_at_request_id,
        )
    can_view_output = is_exact_requester or bool(request.user.is_superuser) or has_manage_scope
    return success(ai_request_to_dict(req, include_output=can_view_output))


# --- budget ----------------------------------------------------------------


@openapi_contract(
    path="/api/v1/ai/budget/",
    operations=AI_BUDGET_CONTRACTS,
)
@csrf_exempt
@require_auth
def budget_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "ai:manage")
        assert_permission_organization_scope(request, permission="ai:manage")
        return success(budget_to_dict(_service().get_budget()))
    if request.method == "PATCH":
        check_perm(request, "ai:manage")
        assert_permission_organization_scope(request, permission="ai:manage")
        data = read_json(request)
        reject_unknown_fields(
            data,
            allowed={"daily_token_limit", "monthly_token_limit", "is_enabled"},
        )
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


@openapi_contract(
    path="/api/v1/ai/exam-generation/",
    operations=(AI_EXAM_GENERATION_CONTRACT,),
)
@csrf_exempt
@require_auth
def exam_generation_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "ai:write")
    principal = _principal(request)
    # A per-request rate cap (10/min per schema+principal) on top of the token budget —
    # stops request flooding before budget accounting runs (mirrors AIGenerationThrottle).
    check_rate(
        scope="ai_generation",
        key=f"{current_schema()}:{principal.kind}:{principal.principal_id}",
        limit=10,
        window=60,
    )
    data = read_json(request)
    reject_unknown_fields(
        data,
        allowed={"subject_id", "exam_type", "question_count", "difficulty"},
    )
    subject_id = _int_value(_require(data, "subject_id"), "subject_id", min_value=1)
    exam_type = _str_value(_require(data, "exam_type"), "exam_type", max_length=32)
    question_count = _int_value(
        _require(data, "question_count"), "question_count", min_value=1, max_value=100
    )
    difficulty = _choice_value(_require(data, "difficulty"), "difficulty", ("easy", "medium", "hard"))
    ai_request = _service().request_exam_generation(
        requested_by=request.user,
        requested_principal=principal,
        subject_id=subject_id,
        exam_type=exam_type,
        question_count=question_count,
        difficulty=difficulty,
    )
    return success({"request_id": ai_request.pk}, status=202)


# --- usage report ----------------------------------------------------------


@openapi_contract(
    path="/api/v1/ai/usage-report/",
    operations=AI_USAGE_REPORT_CONTRACTS,
)
@csrf_exempt
@require_auth
def usage_report_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "ai:manage")
    assert_permission_organization_scope(request, permission="ai:manage")
    unknown = sorted(set(request.GET) - {"month"})
    if unknown:
        raise ValidationException(
            "Unsupported query parameter.",
            code="invalid_query_param",
            fields={name: ["This query parameter is not supported."] for name in unknown},
        )
    start, end = _month_bounds(request.GET.get("month"))
    return success(_service().usage_report(start=start, end=end))
