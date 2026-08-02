"""Strict, permission-scoped payroll HTTP contract."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.payroll.dto import (
    AdjustmentCreateDTO,
    ExportCreateDTO,
    PaymentReconciliationDTO,
    PayrollPeriodCreateDTO,
    PreviewFilterDTO,
    ReversalDTO,
)
from apps.payroll.interfaces.services import IPayrollService
from apps.payroll.models import PayrollAdjustment, PayrollPeriod
from apps.payroll.openapi_contracts import (
    ADJUSTMENT_APPROVE_OPERATION,
    ADJUSTMENT_DETAIL_OPERATIONS,
    ADJUSTMENT_EVENTS_OPERATIONS,
    ADJUSTMENT_REJECT_OPERATION,
    ADJUSTMENTS_OPERATIONS,
    APPROVE_OPERATION,
    DISBURSEMENTS_OPERATIONS,
    EXPORT_DETAIL_OPERATIONS,
    EXPORTS_OPERATIONS,
    LINES_OPERATIONS,
    MY_PAYSLIP_DETAIL_OPERATIONS,
    MY_PAYSLIPS_OPERATIONS,
    PAYSLIP_DETAIL_OPERATIONS,
    PERIOD_DETAIL_OPERATIONS,
    PERIOD_EVENTS_OPERATIONS,
    PERIODS_OPERATIONS,
    PREVIEW_OPERATION,
    RECONCILE_OPERATION,
    RECONCILIATION_DETAIL_OPERATIONS,
    RECONCILIATIONS_OPERATIONS,
    REJECT_OPERATION,
    REVERSAL_OPERATION,
    RUN_OPERATION,
    TOTALS_OPERATIONS,
)
from apps.payroll.presenters import (
    adjustment_event_to_dict,
    adjustment_to_dict,
    disbursement_to_dict,
    export_to_dict,
    line_to_dict,
    payslip_to_dict,
    period_event_to_dict,
    period_to_dict,
    preview_to_dict,
    reconciliation_to_dict,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import (
    decimal_field,
    int_field,
    read_json,
    reject_unknown_fields,
    trimmed_str_field,
)
from core.listing import (
    apply_date_range_filters,
    apply_filters,
    paginate,
    parse_date_range_filters,
    positive_int_filter,
    validate_pagination_filters,
)
from core.openapi_contracts import openapi_contract
from core.permissions import get_user_roles
from core.responses import created, error, paginated, success
from core.role_principals import request_role_principal


def _service() -> IPayrollService:
    return container.resolve(IPayrollService)  # type: ignore[type-abstract]


def _roles(request: HttpRequest):
    return get_user_roles(request)


def _principal(request: HttpRequest):
    return request_role_principal(request, allowed_kinds={"staff"})


def _unknown_query(request: HttpRequest, *, allowed: set[str]) -> None:
    unknown = sorted(set(request.GET) - allowed)
    duplicate = sorted(key for key, values in request.GET.lists() if len(values) != 1)
    errors = {
        **{key: ["This query parameter is not supported."] for key in unknown},
        **{key: ["Provide this query parameter once."] for key in duplicate},
    }
    if errors:
        raise ValidationException(code="validation_error", fields=errors)


def _reject_empty_query(request: HttpRequest, *names: str) -> None:
    """Reject present-but-empty decision filters instead of broadening a read."""
    errors = {
        name: ["Provide a non-empty value."]
        for name in names
        if name in request.GET and request.GET.get(name) == ""
    }
    if errors:
        raise ValidationException(code="validation_error", fields=errors)


def _date(data: dict[str, Any], name: str, *, required: bool = False) -> dt.date | None:
    if name not in data or data[name] in (None, ""):
        if required:
            raise ValidationException(code="validation_error", fields={name: ["This field is required."]})
        return None
    value = data[name]
    if not isinstance(value, str):
        raise ValidationException(code="validation_error", fields={name: ["Use YYYY-MM-DD."]})
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValidationException(
            code="validation_error", fields={name: ["Use a valid YYYY-MM-DD date."]}
        ) from None


def _datetime(data: dict[str, Any], name: str) -> dt.datetime:
    value = data.get(name)
    if not isinstance(value, str):
        raise ValidationException(
            code="validation_error",
            fields={name: ["Use an ISO-8601 timestamp with an offset."]},
        )
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationException(
            code="validation_error",
            fields={name: ["Use an ISO-8601 timestamp with an offset."]},
        ) from None
    if parsed.tzinfo is None:
        raise ValidationException(
            code="validation_error",
            fields={name: ["Use an ISO-8601 timestamp with an offset."]},
        )
    return parsed


def _decimal_string(data: dict[str, Any], name: str):
    if name not in data:
        raise ValidationException(code="validation_error", fields={name: ["This field is required."]})
    if not isinstance(data[name], str) or re.fullmatch(r"\d{1,16}(?:\.\d{1,2})?", data[name]) is None:
        raise ValidationException(
            code="validation_error",
            fields={name: ["Use a decimal string with at most two decimal places."]},
        )
    value = decimal_field(data, name, max_digits=18)
    if value is None:
        raise ValidationException(code="validation_error", fields={name: ["This field is required."]})
    return value


def _teacher_ids(data: dict[str, Any]) -> tuple[int, ...]:
    value = data.get("teacher_ids", [])
    if not isinstance(value, list) or len(value) > 500:
        raise ValidationException(
            code="validation_error", fields={"teacher_ids": ["Use a list of at most 500 IDs."]}
        )
    parsed: list[int] = []
    for item in value:
        teacher_id = int_field({"teacher": item}, "teacher", required=True, min_value=1)
        assert teacher_id is not None
        parsed.append(teacher_id)
    if len(parsed) != len(set(parsed)):
        raise ValidationException(code="validation_error", fields={"teacher_ids": ["IDs must be unique."]})
    return tuple(sorted(parsed))


def _period(request: HttpRequest, pk: int, *, permission: str) -> PayrollPeriod:
    period = _service().period(
        roles=_roles(request),
        permission=permission,
        period_id=pk,
        is_superuser=bool(request.user.is_superuser),
    )
    if period is None:
        raise NotFoundException(code="not_found")
    return period


@openapi_contract(path="/api/v1/payroll/periods/", operations=PERIODS_OPERATIONS)
@csrf_exempt
@require_auth
def periods_view(request: HttpRequest) -> HttpResponse:
    if request.method in {"GET", "HEAD"}:
        check_perm(request, "compensation:read")
        _unknown_query(
            request,
            allowed={
                "page",
                "page_size",
                "status",
                "branch",
                "department",
                "date_from",
                "date_to",
                "ordering",
            },
        )
        validate_pagination_filters(request)
        _reject_empty_query(
            request,
            "status",
            "branch",
            "department",
            "date_from",
            "date_to",
            "ordering",
        )
        queryset = _service().periods(
            roles=_roles(request),
            permission="compensation:read",
            is_superuser=bool(request.user.is_superuser),
        )
        queryset = apply_filters(
            request,
            queryset,
            filter_fields=("status", "branch", "department"),
            ordering_fields=("period_start", "period_end", "pay_date", "created_at"),
            default_ordering="-period_start",
        )
        date_from, date_to = parse_date_range_filters(request)
        queryset = apply_date_range_filters(
            queryset,
            field="period_start",
            date_from=date_from,
            date_to=date_to,
        )
        items, total, page, size = paginate(request, queryset)
        return paginated(
            [period_to_dict(item) for item in items],
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        check_perm(request, "compensation:run")
        body = read_json(request)
        reject_unknown_fields(
            body,
            allowed={
                "branch",
                "department",
                "label",
                "period_start",
                "period_end",
                "pay_date",
                "currency",
                "correction_of",
                "correction_reason",
            },
        )
        branch = int_field(body, "branch", required=True, min_value=1)
        assert branch is not None
        dto = PayrollPeriodCreateDTO(
            branch_id=branch,
            department_id=int_field(body, "department", min_value=1),
            label=trimmed_str_field(body, "label", required=True, max_length=120),
            period_start=_date(body, "period_start", required=True),  # type: ignore[arg-type]
            period_end=_date(body, "period_end", required=True),  # type: ignore[arg-type]
            pay_date=_date(body, "pay_date"),
            currency=trimmed_str_field(body, "currency", default="UZS", max_length=3),
            correction_of_id=int_field(body, "correction_of", min_value=1),
            correction_reason=trimmed_str_field(body, "correction_reason", max_length=255),
        )
        if not dto.label:
            raise ValidationException(code="validation_error", fields={"label": ["This field is required."]})
        period = _service().create_period(
            dto=dto,
            actor=request.user,
            principal=_principal(request),
            roles=_roles(request),
        )
        return created(period_to_dict(period))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(path="/api/v1/payroll/periods/{pk}/", operations=PERIOD_DETAIL_OPERATIONS)
@csrf_exempt
@require_auth
def period_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(request, allowed=set())
    return success(period_to_dict(_period(request, pk, permission="compensation:read")))


@openapi_contract(path="/api/v1/payroll/periods/{pk}/preview/", operations=(PREVIEW_OPERATION,))
@csrf_exempt
@require_auth
def period_preview_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:run")
    body = read_json(request)
    reject_unknown_fields(body, allowed={"teacher_ids"})
    period = _period(request, pk, permission="compensation:run")
    return success(
        preview_to_dict(_service().preview(period=period, filters=PreviewFilterDTO(_teacher_ids(body))))
    )


@openapi_contract(path="/api/v1/payroll/periods/{pk}/run/", operations=(RUN_OPERATION,))
@csrf_exempt
@require_auth
def period_run_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:run")
    body = read_json(request)
    reject_unknown_fields(body, allowed={"teacher_ids"})
    period = _period(request, pk, permission="compensation:run")
    result = _service().run(
        period=period,
        filters=PreviewFilterDTO(_teacher_ids(body)),
        actor=request.user,
        principal=_principal(request),
        idempotency_key=request.headers.get("Idempotency-Key", ""),
    )
    return success(period_to_dict(result))


def _decision_body(body: dict[str, Any]) -> str:
    reject_unknown_fields(body, allowed={"note"})
    return trimmed_str_field(body, "note", max_length=255)


@openapi_contract(path="/api/v1/payroll/periods/{pk}/approve/", operations=(APPROVE_OPERATION,))
@csrf_exempt
@require_auth
def period_approve_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:approve")
    period = _period(request, pk, permission="compensation:approve")
    result = _service().approve(
        period=period,
        actor=request.user,
        principal=_principal(request),
        note=_decision_body(read_json(request)),
        idempotency_key=request.headers.get("Idempotency-Key", ""),
    )
    return success(period_to_dict(result))


@openapi_contract(path="/api/v1/payroll/periods/{pk}/reject/", operations=(REJECT_OPERATION,))
@csrf_exempt
@require_auth
def period_reject_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:approve")
    period = _period(request, pk, permission="compensation:approve")
    result = _service().reject(
        period=period,
        actor=request.user,
        principal=_principal(request),
        note=_decision_body(read_json(request)),
        idempotency_key=request.headers.get("Idempotency-Key", ""),
    )
    return success(period_to_dict(result))


@openapi_contract(path="/api/v1/payroll/periods/{pk}/lines/", operations=LINES_OPERATIONS)
@csrf_exempt
@require_auth
def period_lines_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(
        request,
        allowed={"page", "page_size", "teacher", "payment_state", "ordering"},
    )
    validate_pagination_filters(request)
    _reject_empty_query(request, "teacher", "payment_state", "ordering")
    period = _period(request, pk, permission="compensation:read")
    queryset = _service().lines(period=period)
    teacher_id = positive_int_filter(request, "teacher")
    if teacher_id is not None:
        queryset = queryset.filter(teacher_id=teacher_id)
    payment_state = request.GET.get("payment_state")
    if payment_state:
        if payment_state == "unpaid":
            queryset = queryset.filter(paid_amount_uzs=0)  # type: ignore[misc]
        elif payment_state == "paid":
            queryset = queryset.filter(outstanding_amount_uzs=0)  # type: ignore[misc]
        elif payment_state == "partial":
            queryset = queryset.filter(  # type: ignore[misc]
                paid_amount_uzs__gt=0,
                outstanding_amount_uzs__gt=0,
            )
        else:
            raise ValidationException(
                code="validation_error",
                fields={"payment_state": ["Choose unpaid, partial, or paid."]},
            )
    ordering = request.GET.get("ordering")
    if ordering:
        field = ordering[1:] if ordering.startswith("-") else ordering
        if field not in {"teacher_name_snapshot", "net_amount_uzs", "created_at"}:
            raise ValidationException(code="validation_error", fields={"ordering": ["Unsupported ordering."]})
        queryset = queryset.order_by(ordering)
    items, total, page, size = paginate(request, queryset)
    return paginated([line_to_dict(item) for item in items], total=total, page=page, page_size=size)


@openapi_contract(
    path="/api/v1/payroll/periods/{pk}/events/",
    operations=PERIOD_EVENTS_OPERATIONS,
)
@csrf_exempt
@require_auth
def period_events_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(
        request,
        allowed={"page", "page_size", "action", "date_from", "date_to"},
    )
    validate_pagination_filters(request)
    _reject_empty_query(request, "action", "date_from", "date_to")
    period = _period(request, pk, permission="compensation:read")
    queryset = apply_filters(
        request,
        _service().period_events(period=period),
        filter_fields=("action",),
        default_ordering="created_at",
    )
    date_from, date_to = parse_date_range_filters(request)
    queryset = apply_date_range_filters(
        queryset,
        field="created_at",
        date_from=date_from,
        date_to=date_to,
        datetime_field=True,
    )
    items, total, page, size = paginate(request, queryset)
    return paginated(
        [period_event_to_dict(item) for item in items],
        total=total,
        page=page,
        page_size=size,
    )


@openapi_contract(path="/api/v1/payroll/periods/{pk}/totals/", operations=TOTALS_OPERATIONS)
@csrf_exempt
@require_auth
def period_totals_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(request, allowed=set())
    period = _period(request, pk, permission="compensation:read")
    return success(
        {
            "period": period.pk,
            "currency": period.currency,
            "line_count": period.line_count,
            "base_total_uzs": str(period.base_total_uzs),
            "bonus_total_uzs": str(period.bonus_total_uzs),
            "deduction_total_uzs": str(period.deduction_total_uzs),
            "net_total_uzs": str(period.net_total_uzs),
            "paid_total_uzs": str(period.paid_total_uzs),
            "outstanding_total_uzs": str(period.net_total_uzs - period.paid_total_uzs),
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        }
    )


@openapi_contract(path="/api/v1/payroll/adjustments/", operations=ADJUSTMENTS_OPERATIONS)
@csrf_exempt
@require_auth
def adjustments_view(request: HttpRequest) -> HttpResponse:
    if request.method in {"GET", "HEAD"}:
        check_perm(request, "compensation:read")
        _unknown_query(
            request,
            allowed={
                "page",
                "page_size",
                "state",
                "kind",
                "teacher",
                "branch",
                "department",
                "date_from",
                "date_to",
                "ordering",
            },
        )
        validate_pagination_filters(request)
        _reject_empty_query(
            request,
            "state",
            "kind",
            "teacher",
            "branch",
            "department",
            "date_from",
            "date_to",
            "ordering",
        )
        queryset = _service().adjustments(
            roles=_roles(request),
            permission="compensation:read",
            is_superuser=bool(request.user.is_superuser),
        )
        queryset = apply_filters(
            request,
            queryset,
            filter_fields=("state", "kind", "teacher", "branch", "department"),
            ordering_fields=("created_at", "amount_uzs", "effective_period_start"),
            default_ordering="-created_at",
        )
        date_from, date_to = parse_date_range_filters(request)
        queryset = apply_date_range_filters(
            queryset,
            field="effective_period_start",
            date_from=date_from,
            date_to=date_to,
        )
        items, total, page, size = paginate(request, queryset)
        return paginated(
            [adjustment_to_dict(item) for item in items],
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        check_perm(request, "compensation:write")
        body = read_json(request)
        reject_unknown_fields(
            body,
            allowed={
                "teacher",
                "kind",
                "amount_uzs",
                "currency",
                "effective_period_start",
                "effective_period_end",
                "reason",
            },
        )
        teacher_id = int_field(body, "teacher", required=True, min_value=1)
        assert teacher_id is not None
        amount = _decimal_string(body, "amount_uzs")
        adjustment = _service().create_adjustment(
            dto=AdjustmentCreateDTO(
                teacher_id=teacher_id,
                kind=trimmed_str_field(body, "kind", required=True, max_length=16),
                amount_uzs=amount,
                currency=trimmed_str_field(body, "currency", default="UZS", max_length=3),
                effective_period_start=_date(  # type: ignore[arg-type]
                    body, "effective_period_start", required=True
                ),
                effective_period_end=_date(  # type: ignore[arg-type]
                    body, "effective_period_end", required=True
                ),
                reason=trimmed_str_field(body, "reason", required=True, max_length=255),
                idempotency_key=request.headers.get("Idempotency-Key", ""),
            ),
            actor=request.user,
            principal=_principal(request),
            roles=_roles(request),
        )
        return created(adjustment_to_dict(adjustment))
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _adjustment(request: HttpRequest, pk: int, *, permission: str) -> PayrollAdjustment:
    row = _service().adjustment(
        roles=_roles(request),
        permission=permission,
        adjustment_id=pk,
        is_superuser=bool(request.user.is_superuser),
    )
    if row is None:
        raise NotFoundException(code="not_found")
    return row


@openapi_contract(path="/api/v1/payroll/adjustments/{pk}/", operations=ADJUSTMENT_DETAIL_OPERATIONS)
@csrf_exempt
@require_auth
def adjustment_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(request, allowed=set())
    return success(adjustment_to_dict(_adjustment(request, pk, permission="compensation:read")))


@openapi_contract(
    path="/api/v1/payroll/adjustments/{pk}/events/",
    operations=ADJUSTMENT_EVENTS_OPERATIONS,
)
@csrf_exempt
@require_auth
def adjustment_events_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(
        request,
        allowed={"page", "page_size", "action", "date_from", "date_to"},
    )
    validate_pagination_filters(request)
    _reject_empty_query(request, "action", "date_from", "date_to")
    adjustment = _adjustment(request, pk, permission="compensation:read")
    queryset = apply_filters(
        request,
        _service().adjustment_events(adjustment=adjustment),
        filter_fields=("action",),
        default_ordering="created_at",
    )
    date_from, date_to = parse_date_range_filters(request)
    queryset = apply_date_range_filters(
        queryset,
        field="created_at",
        date_from=date_from,
        date_to=date_to,
        datetime_field=True,
    )
    items, total, page, size = paginate(request, queryset)
    return paginated(
        [adjustment_event_to_dict(item) for item in items],
        total=total,
        page=page,
        page_size=size,
    )


def _decide_adjustment(
    request: HttpRequest,
    pk: int,
    *,
    approve: bool,
    body: dict[str, Any],
) -> HttpResponse:
    check_perm(request, "compensation:approve")
    row = _adjustment(request, pk, permission="compensation:approve")
    result = _service().decide_adjustment(
        adjustment=row,
        approve=approve,
        actor=request.user,
        principal=_principal(request),
        note=_decision_body(body),
        idempotency_key=request.headers.get("Idempotency-Key", ""),
    )
    return success(adjustment_to_dict(result))


@openapi_contract(
    path="/api/v1/payroll/adjustments/{pk}/approve/",
    operations=(ADJUSTMENT_APPROVE_OPERATION,),
)
@csrf_exempt
@require_auth
def adjustment_approve_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return _decide_adjustment(request, pk, approve=True, body=read_json(request))


@openapi_contract(
    path="/api/v1/payroll/adjustments/{pk}/reject/",
    operations=(ADJUSTMENT_REJECT_OPERATION,),
)
@csrf_exempt
@require_auth
def adjustment_reject_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return _decide_adjustment(request, pk, approve=False, body=read_json(request))


@openapi_contract(path="/api/v1/payroll/periods/{pk}/reconcile/", operations=(RECONCILE_OPERATION,))
@csrf_exempt
@require_auth
def period_reconcile_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:disburse")
    period = _period(request, pk, permission="compensation:disburse")
    body = read_json(request)
    reject_unknown_fields(
        body,
        allowed={
            "line_item",
            "amount_uzs",
            "payment_method",
            "external_reference",
            "paid_at",
        },
    )
    line_id = int_field(body, "line_item", required=True, min_value=1)
    method_id = int_field(body, "payment_method", required=True, min_value=1)
    amount = _decimal_string(body, "amount_uzs")
    assert line_id is not None
    assert method_id is not None
    result = _service().reconcile_payment(
        period=period,
        dto=PaymentReconciliationDTO(
            line_item_id=line_id,
            amount_uzs=amount,
            payment_method_id=method_id,
            external_reference=trimmed_str_field(body, "external_reference", required=True, max_length=128),
            paid_at=_datetime(body, "paid_at"),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        ),
        actor=request.user,
        principal=_principal(request),
    )
    return created(reconciliation_to_dict(result))


@openapi_contract(
    path="/api/v1/payroll/periods/{pk}/reconciliations/",
    operations=RECONCILIATIONS_OPERATIONS,
)
@csrf_exempt
@require_auth
def period_reconciliations_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(
        request,
        allowed={
            "page",
            "page_size",
            "kind",
            "line_item",
            "date_from",
            "date_to",
            "ordering",
        },
    )
    validate_pagination_filters(request)
    _reject_empty_query(request, "kind", "line_item", "date_from", "date_to", "ordering")
    period = _period(request, pk, permission="compensation:read")
    queryset = apply_filters(
        request,
        _service().reconciliations(period=period),
        filter_fields=("kind", "line_item"),
        ordering_fields=("paid_at", "created_at", "amount_uzs"),
        default_ordering="created_at",
    )
    date_from, date_to = parse_date_range_filters(request)
    queryset = apply_date_range_filters(
        queryset,
        field="paid_at",
        date_from=date_from,
        date_to=date_to,
        datetime_field=True,
    )
    items, total, page, size = paginate(request, queryset)
    return paginated(
        [reconciliation_to_dict(item) for item in items],
        total=total,
        page=page,
        page_size=size,
    )


@openapi_contract(path="/api/v1/payroll/disbursements/", operations=DISBURSEMENTS_OPERATIONS)
@csrf_exempt
@require_auth
def disbursements_view(request: HttpRequest) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:disburse")
    _unknown_query(
        request,
        allowed={
            "page",
            "page_size",
            "branch",
            "department",
            "date_from",
            "date_to",
            "ordering",
        },
    )
    validate_pagination_filters(request)
    _reject_empty_query(
        request,
        "branch",
        "department",
        "date_from",
        "date_to",
        "ordering",
    )
    queryset = _service().payable_lines(
        roles=_roles(request),
        permission="compensation:disburse",
        is_superuser=bool(request.user.is_superuser),
    )
    branch_id = positive_int_filter(request, "branch")
    department_id = positive_int_filter(request, "department")
    if branch_id is not None:
        queryset = queryset.filter(branch_at_run_id=branch_id)
    if department_id is not None:
        queryset = queryset.filter(department_at_run_id=department_id)
    date_from, date_to = parse_date_range_filters(request)
    queryset = apply_date_range_filters(
        queryset,
        field="period__pay_date",
        date_from=date_from,
        date_to=date_to,
    )
    ordering = request.GET.get("ordering", "pay_date")
    descending = ordering.startswith("-")
    public_field = ordering[1:] if descending else ordering
    ordering_map = {
        "pay_date": "period__pay_date",
        "teacher_name": "teacher_name_snapshot",
        "outstanding_amount_uzs": "outstanding_amount_uzs",
    }
    if public_field not in ordering_map:
        raise ValidationException(
            code="validation_error",
            fields={"ordering": ["Unsupported ordering."]},
        )
    database_field = ordering_map[public_field]
    queryset = queryset.order_by(f"-{database_field}" if descending else database_field)
    items, total, page, size = paginate(request, queryset)
    return paginated(
        [disbursement_to_dict(item) for item in items],
        total=total,
        page=page,
        page_size=size,
    )


@openapi_contract(
    path="/api/v1/payroll/reconciliations/{pk}/",
    operations=RECONCILIATION_DETAIL_OPERATIONS,
)
@csrf_exempt
@require_auth
def reconciliation_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(request, allowed=set())
    row = _service().reconciliation(
        roles=_roles(request),
        permission="compensation:read",
        reconciliation_id=pk,
        is_superuser=bool(request.user.is_superuser),
    )
    if row is None:
        raise NotFoundException(code="not_found")
    return success(reconciliation_to_dict(row))


@openapi_contract(path="/api/v1/payroll/reconciliations/{pk}/reverse/", operations=(REVERSAL_OPERATION,))
@csrf_exempt
@require_auth
def reconciliation_reverse_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:disburse")
    row = _service().reconciliation(
        roles=_roles(request),
        permission="compensation:disburse",
        reconciliation_id=pk,
        is_superuser=bool(request.user.is_superuser),
    )
    if row is None:
        raise NotFoundException(code="not_found")
    body = read_json(request)
    reject_unknown_fields(body, allowed={"external_reference", "paid_at", "reason"})
    result = _service().reverse_payment(
        reconciliation=row,
        dto=ReversalDTO(
            external_reference=trimmed_str_field(body, "external_reference", required=True, max_length=128),
            paid_at=_datetime(body, "paid_at"),
            reason=trimmed_str_field(body, "reason", required=True, max_length=255),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        ),
        actor=request.user,
        principal=_principal(request),
    )
    return created(reconciliation_to_dict(result))


@openapi_contract(path="/api/v1/payroll/periods/{pk}/exports/", operations=EXPORTS_OPERATIONS)
@csrf_exempt
@require_auth
def period_exports_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in {"GET", "HEAD"}:
        check_perm(request, "compensation:read")
        _unknown_query(request, allowed={"page", "page_size"})
        validate_pagination_filters(request)
        period = _period(request, pk, permission="compensation:read")
        items, total, page, size = paginate(request, _service().exports(period=period))
        return paginated(
            [export_to_dict(item) for item in items],
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        check_perm(request, "compensation:read")
        period = _period(request, pk, permission="compensation:read")
        body = read_json(request)
        reject_unknown_fields(body, allowed={"format", "teacher", "payment_state"})
        export = _service().request_export(
            period=period,
            dto=ExportCreateDTO(
                format=trimmed_str_field(body, "format", required=True, max_length=8),
                teacher_id=int_field(body, "teacher", min_value=1),
                payment_state=(
                    trimmed_str_field(body, "payment_state", max_length=16)
                    if "payment_state" in body
                    else None
                ),
                idempotency_key=request.headers.get("Idempotency-Key", ""),
            ),
            actor=request.user,
            principal=_principal(request),
        )
        return created(export_to_dict(export))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/payroll/periods/{pk}/exports/{export_id}/",
    operations=EXPORT_DETAIL_OPERATIONS,
)
@csrf_exempt
@require_auth
def period_export_detail_view(request: HttpRequest, pk: int, export_id: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(request, allowed=set())
    period = _period(request, pk, permission="compensation:read")
    export = _service().export(period=period, export_id=export_id)
    if export is None:
        raise NotFoundException(code="not_found")
    return success(export_to_dict(export, include_download=request.method == "GET"))


@openapi_contract(path="/api/v1/payroll/payslips/mine/", operations=MY_PAYSLIPS_OPERATIONS)
@csrf_exempt
@require_auth
def payslips_mine_view(request: HttpRequest) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    _unknown_query(request, allowed={"page", "page_size"})
    validate_pagination_filters(request)
    principal = request_role_principal(request, allowed_kinds={"teacher"})
    items, total, page, size = paginate(request, _service().self_payslips(teacher_id=principal.principal_id))
    return paginated([payslip_to_dict(item) for item in items], total=total, page=page, page_size=size)


@openapi_contract(path="/api/v1/payroll/payslips/mine/{pk}/", operations=MY_PAYSLIP_DETAIL_OPERATIONS)
@csrf_exempt
@require_auth
def payslip_mine_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    _unknown_query(request, allowed=set())
    principal = request_role_principal(request, allowed_kinds={"teacher"})
    payslip = _service().self_payslip(teacher_id=principal.principal_id, payslip_id=pk)
    if payslip is None:
        raise NotFoundException(code="not_found")
    return success(payslip_to_dict(payslip))


@openapi_contract(path="/api/v1/payroll/payslips/{pk}/", operations=PAYSLIP_DETAIL_OPERATIONS)
@csrf_exempt
@require_auth
def payslip_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in {"GET", "HEAD"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:read")
    _unknown_query(request, allowed=set())
    payslip = _service().payslip_for_reader(
        roles=_roles(request),
        permission="compensation:read",
        payslip_id=pk,
        is_superuser=bool(request.user.is_superuser),
    )
    if payslip is None:
        raise NotFoundException(code="not_found")
    return success(payslip_to_dict(payslip))
