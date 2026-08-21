"""Executable OpenAPI contracts for every payroll operation."""

from __future__ import annotations

from typing import Any

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)


def _errors(*, conflict: bool = False, unprocessable: bool = False) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "400": error_response("The request DTO or query is invalid."),
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "402": error_response("The tenant subscription does not include this capability."),
        "403": error_response("The session lacks the required compensation capability."),
        "404": error_response("The object is absent or outside the exact authorized scope."),
        "405": error_response("The HTTP method is not supported by this operation path."),
        "429": error_response("The authenticated request rate limit was exceeded."),
        "503": error_response("Payroll or a required dependency is temporarily unavailable."),
    }
    if conflict:
        responses["409"] = error_response("The operation conflicts with immutable workflow state.")
    if unprocessable:
        responses["422"] = error_response("The payroll evidence cannot produce a valid result.")
    return responses


def read_operation(
    method: str,
    *,
    summary: str,
    schema: str,
    operation_id: str,
    permission: str | None = "compensation:read",
    parameters: tuple[dict[str, Any], ...] = (),
) -> OperationContract:
    return OperationContract(
        method=method,
        summary=summary,
        description=(
            "The queryset is intersected with the exact branch/department membership that "
            "supplies the stated capability; out-of-scope identifiers are indistinguishable "
            "from absent records. Unknown or duplicate query parameters are rejected."
        ),
        permission=permission,
        security=SESSION_SECURITY,
        parameters=parameters,
        responses={
            "200": json_response("Payroll data.", schema) if method == "GET" else {"description": "Visible."},
            **_errors(),
        },
        operation_id=operation_id,
    )


IDEMPOTENCY_HEADER = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": True,
    "schema": {"type": "string", "minLength": 16, "maxLength": 128},
    "description": "Visible ASCII retry key. Only its tenant/principal-scoped SHA-256 hash is stored.",
}

PAGE_PARAMETERS = (
    {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
    {
        "name": "page_size",
        "in": "query",
        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)


def _query(name: str, schema: dict[str, Any], description: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "in": "query",
        "required": False,
        "schema": schema,
        **({"description": description} if description else {}),
    }


PERIOD_FILTER_PARAMETERS = (
    *PAGE_PARAMETERS,
    _query(
        "status",
        {
            "type": "string",
            "enum": [
                "draft",
                "pending_approval",
                "approved",
                "rejected",
                "payment_in_progress",
                "paid",
            ],
        },
    ),
    _query("branch", {"type": "integer", "minimum": 1}),
    _query("department", {"type": "integer", "minimum": 1}),
    _query("date_from", {"type": "string", "format": "date"}),
    _query("date_to", {"type": "string", "format": "date"}),
    _query(
        "ordering",
        {
            "type": "string",
            "enum": [
                "period_start",
                "-period_start",
                "period_end",
                "-period_end",
                "pay_date",
                "-pay_date",
                "created_at",
                "-created_at",
            ],
        },
    ),
)
LINE_FILTER_PARAMETERS = (
    *PAGE_PARAMETERS,
    _query("teacher", {"type": "integer", "minimum": 1}),
    _query("payment_state", {"type": "string", "enum": ["unpaid", "partial", "paid"]}),
    _query(
        "ordering",
        {
            "type": "string",
            "enum": [
                "teacher_name_snapshot",
                "-teacher_name_snapshot",
                "net_amount_uzs",
                "-net_amount_uzs",
                "created_at",
                "-created_at",
            ],
        },
    ),
)
DISBURSEMENT_FILTER_PARAMETERS = (
    *PAGE_PARAMETERS,
    _query("branch", {"type": "integer", "minimum": 1}),
    _query("department", {"type": "integer", "minimum": 1}),
    _query("date_from", {"type": "string", "format": "date"}),
    _query("date_to", {"type": "string", "format": "date"}),
    _query(
        "ordering",
        {
            "type": "string",
            "enum": [
                "pay_date",
                "-pay_date",
                "teacher_name",
                "-teacher_name",
                "outstanding_amount_uzs",
                "-outstanding_amount_uzs",
            ],
        },
    ),
)
PERIOD_EVENT_FILTER_PARAMETERS = (
    *PAGE_PARAMETERS,
    _query(
        "action",
        {
            "type": "string",
            "enum": ["run", "approve", "reject", "payment", "reversal"],
        },
    ),
    _query("date_from", {"type": "string", "format": "date"}),
    _query("date_to", {"type": "string", "format": "date"}),
)
ADJUSTMENT_EVENT_FILTER_PARAMETERS = (
    *PAGE_PARAMETERS,
    _query(
        "action",
        {
            "type": "string",
            "enum": ["created", "approved", "rejected", "applied", "released"],
        },
    ),
    _query("date_from", {"type": "string", "format": "date"}),
    _query("date_to", {"type": "string", "format": "date"}),
)
ADJUSTMENT_FILTER_PARAMETERS = (
    *PAGE_PARAMETERS,
    _query("state", {"type": "string", "enum": ["pending", "approved", "rejected", "applied"]}),
    _query("kind", {"type": "string", "enum": ["bonus", "deduction"]}),
    _query("teacher", {"type": "integer", "minimum": 1}),
    _query("branch", {"type": "integer", "minimum": 1}),
    _query("department", {"type": "integer", "minimum": 1}),
    _query("date_from", {"type": "string", "format": "date"}),
    _query("date_to", {"type": "string", "format": "date"}),
    _query(
        "ordering",
        {
            "type": "string",
            "enum": [
                "created_at",
                "-created_at",
                "amount_uzs",
                "-amount_uzs",
                "effective_period_start",
                "-effective_period_start",
            ],
        },
    ),
)
RECONCILIATION_FILTER_PARAMETERS = (
    *PAGE_PARAMETERS,
    _query("kind", {"type": "string", "enum": ["payment", "reversal"]}),
    _query("line_item", {"type": "integer", "minimum": 1}),
    _query("date_from", {"type": "string", "format": "date"}),
    _query("date_to", {"type": "string", "format": "date"}),
    _query(
        "ordering",
        {
            "type": "string",
            "enum": [
                "paid_at",
                "-paid_at",
                "created_at",
                "-created_at",
                "amount_uzs",
                "-amount_uzs",
            ],
        },
    ),
)


def write_operation(
    *,
    summary: str,
    request_schema: str,
    response_schema: str,
    operation_id: str,
    permission: str,
    status: str = "200",
    idempotent: bool = True,
    conflict: bool = True,
    unprocessable: bool = False,
) -> OperationContract:
    return OperationContract(
        method="POST",
        summary=summary,
        description=(
            "This mutation uses a closed JSON DTO, exact role-principal attribution, immutable "
            "scope snapshots, and maker-checker workflow controls. Monetary fields are decimal "
            "strings in major UZS and are never interpreted as binary floats."
        ),
        permission=permission,
        security=UNSAFE_SESSION_SECURITY,
        parameters=(IDEMPOTENCY_HEADER,) if idempotent else (),
        request_body=json_request(request_schema),
        responses={
            status: json_response("Payroll operation completed.", response_schema),
            **_errors(conflict=conflict, unprocessable=unprocessable),
        },
        operation_id=operation_id,
    )


PERIODS_OPERATIONS = (
    read_operation(
        "GET",
        summary="List scoped payroll periods",
        schema="PayrollPeriodListResponse",
        operation_id="get_payroll_periods",
        parameters=PERIOD_FILTER_PARAMETERS,
    ),
    read_operation(
        "HEAD",
        summary="Check scoped payroll-period visibility",
        schema="PayrollPeriodListResponse",
        operation_id="head_payroll_periods",
        parameters=PERIOD_FILTER_PARAMETERS,
    ),
    write_operation(
        summary="Create a payroll period or rejected-run correction",
        request_schema="PayrollPeriodCreateRequest",
        response_schema="PayrollPeriodResponse",
        operation_id="post_payroll_periods",
        permission="compensation:run",
        status="201",
        idempotent=False,
    ),
)

PERIOD_DETAIL_OPERATIONS = tuple(
    read_operation(
        method,
        summary="Read a scoped payroll period",
        schema="PayrollPeriodResponse",
        operation_id=f"{method.lower()}_payroll_period",
    )
    for method in ("GET", "HEAD")
)

PREVIEW_OPERATION = write_operation(
    summary="Preview a bounded payroll run",
    request_schema="PayrollPreviewRequest",
    response_schema="PayrollPreviewResponse",
    operation_id="post_payroll_period_preview",
    permission="compensation:run",
    idempotent=False,
    unprocessable=True,
)
RUN_OPERATION = write_operation(
    summary="Idempotently freeze payroll lines and payslips",
    request_schema="PayrollPreviewRequest",
    response_schema="PayrollPeriodResponse",
    operation_id="post_payroll_period_run",
    permission="compensation:run",
    unprocessable=True,
)
APPROVE_OPERATION = write_operation(
    summary="Approve a frozen payroll batch",
    request_schema="PayrollDecisionRequest",
    response_schema="PayrollPeriodResponse",
    operation_id="post_payroll_period_approve",
    permission="compensation:approve",
)
REJECT_OPERATION = write_operation(
    summary="Reject a frozen payroll batch without deleting evidence",
    request_schema="PayrollRejectionRequest",
    response_schema="PayrollPeriodResponse",
    operation_id="post_payroll_period_reject",
    permission="compensation:approve",
)

LINES_OPERATIONS = tuple(
    read_operation(
        method,
        summary="List immutable payroll lines",
        schema="PayrollLineListResponse",
        operation_id=f"{method.lower()}_payroll_period_lines",
        parameters=LINE_FILTER_PARAMETERS,
    )
    for method in ("GET", "HEAD")
)
PERIOD_EVENTS_OPERATIONS = tuple(
    read_operation(
        method,
        summary="List immutable payroll-period workflow events",
        schema="PayrollPeriodEventListResponse",
        operation_id=f"{method.lower()}_payroll_period_events",
        parameters=PERIOD_EVENT_FILTER_PARAMETERS,
    )
    for method in ("GET", "HEAD")
)
TOTALS_OPERATIONS = tuple(
    read_operation(
        method,
        summary="Read payroll batch totals",
        schema="PayrollTotalsResponse",
        operation_id=f"{method.lower()}_payroll_period_totals",
    )
    for method in ("GET", "HEAD")
)
DISBURSEMENTS_OPERATIONS = tuple(
    read_operation(
        method,
        summary="List scoped outstanding payroll payment instructions",
        schema="PayrollDisbursementListResponse",
        operation_id=f"{method.lower()}_payroll_disbursements",
        permission="compensation:disburse",
        parameters=DISBURSEMENT_FILTER_PARAMETERS,
    )
    for method in ("GET", "HEAD")
)
RECONCILE_OPERATION = write_operation(
    summary="Append a salary-payment reconciliation",
    request_schema="PayrollReconcileRequest",
    response_schema="PayrollReconciliationResponse",
    operation_id="post_payroll_period_reconcile",
    permission="compensation:disburse",
    status="201",
)
REVERSAL_OPERATION = write_operation(
    summary="Append a compensating reconciliation reversal",
    request_schema="PayrollReversalRequest",
    response_schema="PayrollReconciliationResponse",
    operation_id="post_payroll_reconciliation_reverse",
    permission="compensation:disburse",
    status="201",
)
RECONCILIATIONS_OPERATIONS = tuple(
    read_operation(
        method,
        summary="List append-only payroll payment reconciliations",
        schema="PayrollReconciliationListResponse",
        operation_id=f"{method.lower()}_payroll_period_reconciliations",
        parameters=RECONCILIATION_FILTER_PARAMETERS,
    )
    for method in ("GET", "HEAD")
)
RECONCILIATION_DETAIL_OPERATIONS = tuple(
    read_operation(
        method,
        summary="Read a scoped payroll payment reconciliation",
        schema="PayrollReconciliationResponse",
        operation_id=f"{method.lower()}_payroll_reconciliation",
        permission="compensation:read",
    )
    for method in ("GET", "HEAD")
)

ADJUSTMENTS_OPERATIONS = (
    read_operation(
        "GET",
        summary="List scoped compensation adjustments",
        schema="PayrollAdjustmentListResponse",
        operation_id="get_payroll_adjustments",
        parameters=ADJUSTMENT_FILTER_PARAMETERS,
    ),
    read_operation(
        "HEAD",
        summary="Check compensation-adjustment visibility",
        schema="PayrollAdjustmentListResponse",
        operation_id="head_payroll_adjustments",
        parameters=ADJUSTMENT_FILTER_PARAMETERS,
    ),
    write_operation(
        summary="Append a bonus or deduction",
        request_schema="PayrollAdjustmentCreateRequest",
        response_schema="PayrollAdjustmentResponse",
        operation_id="post_payroll_adjustments",
        permission="compensation:write",
        status="201",
    ),
)
ADJUSTMENT_DETAIL_OPERATIONS = tuple(
    read_operation(
        method,
        summary="Read a scoped compensation adjustment",
        schema="PayrollAdjustmentResponse",
        operation_id=f"{method.lower()}_payroll_adjustment",
    )
    for method in ("GET", "HEAD")
)
ADJUSTMENT_EVENTS_OPERATIONS = tuple(
    read_operation(
        method,
        summary="List immutable compensation-adjustment events",
        schema="PayrollAdjustmentEventListResponse",
        operation_id=f"{method.lower()}_payroll_adjustment_events",
        parameters=ADJUSTMENT_EVENT_FILTER_PARAMETERS,
    )
    for method in ("GET", "HEAD")
)
ADJUSTMENT_APPROVE_OPERATION = write_operation(
    summary="Approve a compensation adjustment",
    request_schema="PayrollDecisionRequest",
    response_schema="PayrollAdjustmentResponse",
    operation_id="post_payroll_adjustment_approve",
    permission="compensation:approve",
)
ADJUSTMENT_REJECT_OPERATION = write_operation(
    summary="Reject a compensation adjustment",
    request_schema="PayrollRejectionRequest",
    response_schema="PayrollAdjustmentResponse",
    operation_id="post_payroll_adjustment_reject",
    permission="compensation:approve",
)

EXPORTS_OPERATIONS = (
    read_operation(
        "GET",
        summary="List payroll exports",
        schema="PayrollExportListResponse",
        operation_id="get_payroll_period_exports",
        parameters=PAGE_PARAMETERS,
    ),
    read_operation(
        "HEAD",
        summary="Check payroll export visibility",
        schema="PayrollExportListResponse",
        operation_id="head_payroll_period_exports",
        parameters=PAGE_PARAMETERS,
    ),
    write_operation(
        summary="Queue an exact-filter XLSX or PDF payroll export",
        request_schema="PayrollExportCreateRequest",
        response_schema="PayrollExportResponse",
        operation_id="post_payroll_period_exports",
        permission="compensation:read",
        status="201",
    ),
)
EXPORT_DETAIL_OPERATIONS = tuple(
    read_operation(
        method,
        summary="Read payroll export status and a short-lived download URL",
        schema="PayrollExportResponse",
        operation_id=f"{method.lower()}_payroll_period_export",
    )
    for method in ("GET", "HEAD")
)

PAYSLIP_DETAIL_OPERATIONS = tuple(
    read_operation(
        method,
        summary="Read a scoped immutable payslip",
        schema="PayrollPayslipResponse",
        operation_id=f"{method.lower()}_payroll_payslip",
    )
    for method in ("GET", "HEAD")
)
MY_PAYSLIPS_OPERATIONS = tuple(
    read_operation(
        method,
        summary="List the active teacher principal's own approved payslips",
        schema="PayrollPayslipListResponse",
        operation_id=f"{method.lower()}_payroll_payslips_mine",
        permission=None,
        parameters=PAGE_PARAMETERS,
    )
    for method in ("GET", "HEAD")
)
MY_PAYSLIP_DETAIL_OPERATIONS = tuple(
    read_operation(
        method,
        summary="Read the active teacher principal's own approved payslip",
        schema="PayrollPayslipResponse",
        operation_id=f"{method.lower()}_payroll_payslip_mine",
        permission=None,
    )
    for method in ("GET", "HEAD")
)


def _closed(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        **({"required": list(required)} if required else {}),
    }


_ID = {"type": "integer", "format": "int64", "minimum": 1}
_DATE = {"type": "string", "format": "date"}
_DATETIME = {"type": "string", "format": "date-time"}
_MONEY = {
    "type": "string",
    "pattern": r"^\d{1,16}(\.\d{1,2})?$",
    "maxLength": 19,
    "example": "15000000.00",
}
_CURRENCY = {"type": "string", "enum": ["UZS"]}

PAYROLL_SCHEMAS: dict[str, Any] = {
    "PayrollPeriodCreateRequest": _closed(
        {
            "branch": _ID,
            "department": {**_ID, "nullable": True},
            "label": {"type": "string", "minLength": 1, "maxLength": 120},
            "period_start": _DATE,
            "period_end": _DATE,
            "pay_date": {**_DATE, "nullable": True},
            "currency": _CURRENCY,
            "correction_of": {**_ID, "nullable": True},
            "correction_reason": {"type": "string", "maxLength": 255},
        },
        ("branch", "label", "period_start", "period_end"),
    ),
    "PayrollPreviewRequest": _closed(
        {"teacher_ids": {"type": "array", "maxItems": 500, "uniqueItems": True, "items": _ID}}
    ),
    "PayrollDecisionRequest": _closed({"note": {"type": "string", "maxLength": 255}}),
    "PayrollRejectionRequest": _closed(
        {"note": {"type": "string", "minLength": 1, "maxLength": 255}},
        ("note",),
    ),
    "PayrollAdjustmentCreateRequest": _closed(
        {
            "teacher": _ID,
            "kind": {"type": "string", "enum": ["bonus", "deduction"]},
            "amount_uzs": _MONEY,
            "currency": _CURRENCY,
            "effective_period_start": _DATE,
            "effective_period_end": _DATE,
            "reason": {"type": "string", "minLength": 1, "maxLength": 255},
        },
        (
            "teacher",
            "kind",
            "amount_uzs",
            "effective_period_start",
            "effective_period_end",
            "reason",
        ),
    ),
    "PayrollReconcileRequest": _closed(
        {
            "line_item": _ID,
            "amount_uzs": _MONEY,
            "payment_method": _ID,
            "external_reference": {"type": "string", "minLength": 1, "maxLength": 128},
            "paid_at": _DATETIME,
        },
        ("line_item", "amount_uzs", "payment_method", "external_reference", "paid_at"),
    ),
    "PayrollReversalRequest": _closed(
        {
            "external_reference": {"type": "string", "minLength": 1, "maxLength": 128},
            "paid_at": _DATETIME,
            "reason": {"type": "string", "minLength": 1, "maxLength": 255},
        },
        ("external_reference", "paid_at", "reason"),
    ),
    "PayrollExportCreateRequest": _closed(
        {
            "format": {"type": "string", "enum": ["xlsx", "pdf"]},
            "teacher": {**_ID, "nullable": True},
            "payment_state": {
                "type": "string",
                "enum": ["unpaid", "partial", "paid"],
            },
        },
        ("format",),
    ),
}


def _required(properties: dict[str, Any]) -> dict[str, Any]:
    return _closed(properties, tuple(properties))


_NULLABLE_ID = {**_ID, "nullable": True}
_NULLABLE_STRING = {"type": "string", "nullable": True}
_NULLABLE_DATETIME = {**_DATETIME, "nullable": True}
_PRINCIPAL = _required(
    {
        "kind": {"type": "string", "enum": ["staff", "teacher"]},
        "id": _ID,
    }
)
_NULLABLE_PRINCIPAL = {**_PRINCIPAL, "nullable": True}

PAYROLL_SCHEMAS.update(
    {
        "PayrollPrincipal": _PRINCIPAL,
        "PayrollPayoutPolicy": _required(
            {
                "id": _ID,
                "method": {
                    "type": "string",
                    "enum": ["hourly", "percent_of_collected_tuition", "flat_monthly"],
                },
                "hourly_rate_uzs": {**_MONEY, "nullable": True},
                "flat_amount_uzs": {**_MONEY, "nullable": True},
                "tuition_percent": {**_MONEY, "nullable": True},
                "updated_at": _DATETIME,
            }
        ),
        "PayrollCalculation": _closed(
            {
                "hours": {"type": "string"},
                "hourly_rate_uzs": _MONEY,
                "collected_uzs": _MONEY,
                "tuition_percent": _MONEY,
                "cohort_count": {"type": "integer", "minimum": 0},
                "attribution": {
                    "type": "string",
                    "enum": ["completed_lesson_cohorts"],
                },
                "flat_amount_uzs": _MONEY,
            }
        ),
        "PayrollPeriod": _required(
            {
                "id": _ID,
                "label": {"type": "string"},
                "branch": _ID,
                "branch_name": {"type": "string"},
                "department": _NULLABLE_ID,
                "department_name": _NULLABLE_STRING,
                "period_start": _DATE,
                "period_end": _DATE,
                "pay_date": {**_DATE, "nullable": True},
                "currency": _CURRENCY,
                "organization_timezone": {"type": "string", "minLength": 1, "maxLength": 64},
                "status": {
                    "type": "string",
                    "enum": [
                        "draft",
                        "pending_approval",
                        "approved",
                        "rejected",
                        "payment_in_progress",
                        "paid",
                    ],
                },
                "correction_of": _NULLABLE_ID,
                "correction_reason": {"type": "string"},
                "line_count": {"type": "integer", "minimum": 0},
                "base_total_uzs": _MONEY,
                "bonus_total_uzs": _MONEY,
                "deduction_total_uzs": _MONEY,
                "net_total_uzs": _MONEY,
                "paid_total_uzs": _MONEY,
                "outstanding_total_uzs": _MONEY,
                "created_by": _NULLABLE_ID,
                "created_principal": _PRINCIPAL,
                "run_by": _NULLABLE_ID,
                "run_principal": _NULLABLE_PRINCIPAL,
                "approved_by": _NULLABLE_ID,
                "approved_principal": _NULLABLE_PRINCIPAL,
                "rejected_by": _NULLABLE_ID,
                "rejected_principal": _NULLABLE_PRINCIPAL,
                "decision_note": {"type": "string"},
                "version": {"type": "integer", "minimum": 1},
                "frozen_at": _NULLABLE_DATETIME,
                "decided_at": _NULLABLE_DATETIME,
                "created_at": _DATETIME,
                "updated_at": _DATETIME,
            }
        ),
        "PayrollPreviewError": _required(
            {
                "teacher": _ID,
                "code": {"type": "string"},
            }
        ),
        "PayrollPreviewLine": _required(
            {
                "teacher": _ID,
                "teacher_code": {"type": "string"},
                "teacher_name": {"type": "string"},
                "payout_method": {
                    "type": "string",
                    "enum": ["hourly", "percent_of_collected_tuition", "flat_monthly"],
                },
                "base_amount_uzs": _MONEY,
                "bonus_amount_uzs": _MONEY,
                "deduction_amount_uzs": _MONEY,
                "net_amount_uzs": _MONEY,
                "currency": _CURRENCY,
                "calculation": {"$ref": "#/components/schemas/PayrollCalculation"},
                "approved_adjustment_count": {"type": "integer", "minimum": 0},
            }
        ),
        "PayrollPreview": _required(
            {
                "generated_at": _DATETIME,
                "period": _ID,
                "scope": _required(
                    {
                        "branch": _required({"id": _ID, "name": {"type": "string"}}),
                        "department": {
                            **_required({"id": _ID, "name": {"type": "string"}}),
                            "nullable": True,
                        },
                    }
                ),
                "window": _required(
                    {
                        "period_start": _DATE,
                        "period_end": _DATE,
                        "timezone": {"type": "string", "minLength": 1, "maxLength": 64},
                    }
                ),
                "currency": _CURRENCY,
                "valid": {"type": "boolean"},
                "teacher_count": {"type": "integer", "minimum": 0},
                "line_count": {"type": "integer", "minimum": 0},
                "base_total_uzs": _MONEY,
                "bonus_total_uzs": _MONEY,
                "deduction_total_uzs": _MONEY,
                "net_total_uzs": _MONEY,
                "errors": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/PayrollPreviewError"},
                },
                "lines": {
                    "type": "array",
                    "maxItems": 500,
                    "items": {"$ref": "#/components/schemas/PayrollPreviewLine"},
                },
            }
        ),
        "PayrollLine": _required(
            {
                "id": _ID,
                "period": _ID,
                "payslip": _ID,
                "payslip_number": {"type": "string"},
                "teacher": _ID,
                "teacher_code": {"type": "string"},
                "teacher_name": {"type": "string"},
                "branch_at_run": _ID,
                "branch_at_run_name": {"type": "string"},
                "department_at_run": _NULLABLE_ID,
                "department_at_run_name": _NULLABLE_STRING,
                "payout_method": {
                    "type": "string",
                    "enum": ["hourly", "percent_of_collected_tuition", "flat_monthly"],
                },
                "payout_policy": {"$ref": "#/components/schemas/PayrollPayoutPolicy"},
                "calculation": {"$ref": "#/components/schemas/PayrollCalculation"},
                "base_amount_uzs": _MONEY,
                "bonus_amount_uzs": _MONEY,
                "deduction_amount_uzs": _MONEY,
                "net_amount_uzs": _MONEY,
                "paid_amount_uzs": _MONEY,
                "outstanding_amount_uzs": _MONEY,
                "payment_state": {
                    "type": "string",
                    "enum": ["unpaid", "partial", "paid"],
                },
                "currency": _CURRENCY,
                "created_at": _DATETIME,
            }
        ),
        "PayrollTotals": _required(
            {
                "period": _ID,
                "currency": _CURRENCY,
                "line_count": {"type": "integer", "minimum": 0},
                "base_total_uzs": _MONEY,
                "bonus_total_uzs": _MONEY,
                "deduction_total_uzs": _MONEY,
                "net_total_uzs": _MONEY,
                "paid_total_uzs": _MONEY,
                "outstanding_total_uzs": _MONEY,
                "generated_at": _DATETIME,
            }
        ),
        "PayrollDisbursement": _required(
            {
                "line_item": _ID,
                "period": _ID,
                "period_label": {"type": "string"},
                "pay_date": {**_DATE, "nullable": True},
                "payslip": _ID,
                "payslip_number": {"type": "string"},
                "teacher": _ID,
                "teacher_code": {"type": "string"},
                "teacher_name": {"type": "string"},
                "branch_at_run": _ID,
                "branch_at_run_name": {"type": "string"},
                "department_at_run": _NULLABLE_ID,
                "department_at_run_name": _NULLABLE_STRING,
                "amount_uzs": _MONEY,
                "paid_amount_uzs": _MONEY,
                "outstanding_amount_uzs": _MONEY,
                "currency": _CURRENCY,
            }
        ),
        "PayrollAdjustment": _required(
            {
                "id": _ID,
                "teacher": _ID,
                "teacher_code": {"type": "string"},
                "teacher_name": {"type": "string"},
                "branch": _ID,
                "branch_name": {"type": "string"},
                "department": _NULLABLE_ID,
                "department_name": _NULLABLE_STRING,
                "kind": {"type": "string", "enum": ["bonus", "deduction"]},
                "amount_uzs": _MONEY,
                "currency": _CURRENCY,
                "effective_period_start": _DATE,
                "effective_period_end": _DATE,
                "reason": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected", "applied"],
                },
                "created_by": _NULLABLE_ID,
                "created_principal": _PRINCIPAL,
                "decided_by": _NULLABLE_ID,
                "decided_principal": _NULLABLE_PRINCIPAL,
                "decided_at": _NULLABLE_DATETIME,
                "decision_reason": {"type": "string"},
                "applied_line": _NULLABLE_ID,
                "created_at": _DATETIME,
            }
        ),
        "PayrollReconciliation": _required(
            {
                "id": _ID,
                "line_item": _ID,
                "kind": {"type": "string", "enum": ["payment", "reversal"]},
                "reverses": _NULLABLE_ID,
                "amount_uzs": _MONEY,
                "currency": _CURRENCY,
                "payment_method": _ID,
                "payment_method_name": {"type": "string"},
                "external_reference": {"type": "string"},
                "paid_at": _DATETIME,
                "reason": {"type": "string"},
                "ledger_entry": _ID,
                "recorded_by": _NULLABLE_ID,
                "recorded_principal": _PRINCIPAL,
                "created_at": _DATETIME,
            }
        ),
        "PayrollExport": _required(
            {
                "id": _ID,
                "period": _ID,
                "format": {"type": "string", "enum": ["xlsx", "pdf"]},
                "filters": _closed(
                    {
                        "teacher": _ID,
                        "payment_state": {
                            "type": "string",
                            "enum": ["unpaid", "partial", "paid"],
                        },
                    }
                ),
                "status": {
                    "type": "string",
                    "enum": ["queued", "running", "done", "failed"],
                },
                "file_bytes": {"type": "integer", "format": "int64", "minimum": 0},
                "error_code": {"type": "string"},
                "download_url": {"type": "string", "format": "uri", "nullable": True},
                "download_expires_in": {
                    "type": "integer",
                    "minimum": 1,
                    "nullable": True,
                },
                "created_at": _DATETIME,
                "started_at": _NULLABLE_DATETIME,
                "finished_at": _NULLABLE_DATETIME,
            }
        ),
        "PayrollPayslipSnapshot": _required(
            {
                "period": _required(
                    {
                        "id": _ID,
                        "label": {"type": "string"},
                        "period_start": _DATE,
                        "period_end": _DATE,
                        "pay_date": {**_DATE, "nullable": True},
                        "organization_timezone": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                        },
                    }
                ),
                "teacher": _required(
                    {
                        "id": _ID,
                        "code": {"type": "string"},
                        "name": {"type": "string"},
                    }
                ),
                "currency": _CURRENCY,
                "base_amount_uzs": _MONEY,
                "bonus_amount_uzs": _MONEY,
                "deduction_amount_uzs": _MONEY,
                "net_amount_uzs": _MONEY,
                "calculation": {"$ref": "#/components/schemas/PayrollCalculation"},
                "payout_policy": {"$ref": "#/components/schemas/PayrollPayoutPolicy"},
            }
        ),
        "PayrollPayslip": _required(
            {
                "id": _ID,
                "document_number": {"type": "string"},
                "period": _ID,
                "period_status": {
                    "type": "string",
                    "enum": [
                        "pending_approval",
                        "approved",
                        "rejected",
                        "payment_in_progress",
                        "paid",
                    ],
                },
                "branch_at_run": _ID,
                "branch_at_run_name": {"type": "string"},
                "department_at_run": _NULLABLE_ID,
                "department_at_run_name": _NULLABLE_STRING,
                "snapshot": {"$ref": "#/components/schemas/PayrollPayslipSnapshot"},
                "generated_at": _DATETIME,
            }
        ),
        "PayrollPeriodEvent": _required(
            {
                "id": _ID,
                "period": _ID,
                "action": {
                    "type": "string",
                    "enum": ["run", "approve", "reject", "payment", "reversal"],
                },
                "actor": _NULLABLE_ID,
                "actor_principal": _PRINCIPAL,
                "note": {"type": "string"},
                "created_at": _DATETIME,
            }
        ),
        "PayrollAdjustmentEvent": _required(
            {
                "id": _ID,
                "adjustment": _ID,
                "action": {
                    "type": "string",
                    "enum": ["created", "approved", "rejected", "applied", "released"],
                },
                "actor": _NULLABLE_ID,
                "actor_principal": _PRINCIPAL,
                "note": {"type": "string"},
                "created_at": _DATETIME,
            }
        ),
        "PayrollPagination": _required(
            {
                "total": {"type": "integer", "minimum": 0},
                "page": {"type": "integer", "minimum": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
                "pages": {"type": "integer", "minimum": 0},
                "has_next": {"type": "boolean"},
                "has_prev": {"type": "boolean"},
            }
        ),
    }
)

for response_name, data_name in (
    ("PayrollPeriodResponse", "PayrollPeriod"),
    ("PayrollPreviewResponse", "PayrollPreview"),
    ("PayrollTotalsResponse", "PayrollTotals"),
    ("PayrollAdjustmentResponse", "PayrollAdjustment"),
    ("PayrollReconciliationResponse", "PayrollReconciliation"),
    ("PayrollExportResponse", "PayrollExport"),
    ("PayrollPayslipResponse", "PayrollPayslip"),
):
    PAYROLL_SCHEMAS[response_name] = _closed(
        {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"$ref": f"#/components/schemas/{data_name}"},
        },
        ("success", "data"),
    )

for response_name, item_name in (
    ("PayrollPeriodListResponse", "PayrollPeriod"),
    ("PayrollLineListResponse", "PayrollLine"),
    ("PayrollDisbursementListResponse", "PayrollDisbursement"),
    ("PayrollAdjustmentListResponse", "PayrollAdjustment"),
    ("PayrollExportListResponse", "PayrollExport"),
    ("PayrollPayslipListResponse", "PayrollPayslip"),
    ("PayrollPeriodEventListResponse", "PayrollPeriodEvent"),
    ("PayrollAdjustmentEventListResponse", "PayrollAdjustmentEvent"),
    ("PayrollReconciliationListResponse", "PayrollReconciliation"),
):
    PAYROLL_SCHEMAS[response_name] = _required(
        {
            "success": {"type": "boolean", "enum": [True]},
            "data": {
                "type": "array",
                "items": {"$ref": f"#/components/schemas/{item_name}"},
            },
            "pagination": {"$ref": "#/components/schemas/PayrollPagination"},
        }
    )
