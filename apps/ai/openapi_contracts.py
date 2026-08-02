"""Executable contracts for the AI audit, budget, and generation boundary."""

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


def _read_errors(*, not_found: bool = False) -> dict[str, dict[str, Any]]:
    responses = {
        "400": error_response("A declared query parameter is malformed or unsupported."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The exact principal lacks the required permission or scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    }
    if not_found:
        responses["404"] = error_response("The AI request is outside the caller's exact scope.")
    return responses


_PAGE_PARAMETERS = (
    {
        "name": "page",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1},
    },
    {
        "name": "page_size",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
    },
    {
        "name": "feature",
        "in": "query",
        "required": False,
        "schema": {"$ref": "#/components/schemas/AIFeature"},
    },
    {
        "name": "status",
        "in": "query",
        "required": False,
        "schema": {"$ref": "#/components/schemas/AIRequestStatus"},
    },
    {
        "name": "created_after",
        "in": "query",
        "required": False,
        "schema": {
            "oneOf": [
                {"type": "string", "format": "date"},
                {"type": "string", "format": "date-time"},
            ]
        },
    },
    {
        "name": "created_before",
        "in": "query",
        "required": False,
        "schema": {
            "oneOf": [
                {"type": "string", "format": "date"},
                {"type": "string", "format": "date-time"},
            ]
        },
    },
    {
        "name": "ordering",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["created_at", "-created_at"]},
    },
)


def _requests_read(method: str, *, detail: bool = False) -> OperationContract:
    success_response = (
        {"description": "AI request headers are available; HEAD has no response body."}
        if method == "HEAD"
        else json_response(
            "Scoped AI request." if detail else "Scoped AI request page.",
            "AIRequestResponse" if detail else "AIRequestPageResponse",
        )
    )
    return OperationContract(
        method=method,
        summary=("Read one AI request" if detail else "List scoped AI requests"),
        description=(
            "Reads only requests with immutable role-native attribution inside the caller's current "
            "ai:read scope. Legacy ambiguous rows fail closed. Generated text is omitted from pages "
            "and appears on detail only for the exact requester or an exact-scope ai:manage account."
        ),
        permission="ai:read",
        security=SESSION_SECURITY,
        parameters=() if detail else _PAGE_PARAMETERS,
        responses={
            "200": success_response,
            **_read_errors(not_found=detail),
        },
        operation_id=f"{method.lower()}_ai_request_{'detail' if detail else 'collection'}",
    )


AI_REQUEST_COLLECTION_CONTRACTS = (
    _requests_read("GET"),
    _requests_read("HEAD"),
)
AI_REQUEST_DETAIL_CONTRACTS = (
    _requests_read("GET", detail=True),
    _requests_read("HEAD", detail=True),
)


def _budget_read(method: str) -> OperationContract:
    return OperationContract(
        method=method,
        summary="Read the organization AI budget",
        description=(
            "Requires organization-wide ai:manage. This is an observational snapshot: GET and HEAD "
            "do not provision a budget row or persist counter rollover."
        ),
        permission="ai:manage",
        security=SESSION_SECURITY,
        responses={
            "200": (
                {"description": "AI budget headers are available; HEAD has no response body."}
                if method == "HEAD"
                else json_response("Current effective AI budget.", "AIBudgetResponse")
            ),
            **_read_errors(),
        },
        operation_id=f"{method.lower()}_ai_budget",
    )


AI_BUDGET_CONTRACTS = (
    _budget_read("GET"),
    _budget_read("HEAD"),
    OperationContract(
        method="PATCH",
        summary="Update the organization AI budget",
        description=(
            "Requires organization-wide ai:manage. The DTO is closed and limits are bounded; the "
            "monthly limit cannot be below the daily limit."
        ),
        permission="ai:manage",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request("AIBudgetPatchRequest"),
        responses={
            "200": json_response("AI budget updated.", "AIBudgetResponse"),
            "400": error_response("The closed budget DTO or limit relationship is invalid."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The principal lacks organization-wide ai:manage."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id="patch_ai_budget",
    ),
)


AI_EXAM_GENERATION_CONTRACT = OperationContract(
    method="POST",
    summary="Queue scoped exam generation",
    description=(
        "Validates an active subject and exam type, binds the exact role principal, source scope, "
        "prompt version, and closed generation parameters, reserves budget, and returns a polling ID."
    ),
    permission="ai:write",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("AIExamGenerationRequest"),
    responses={
        "202": json_response("Generation accepted for asynchronous processing.", "AIQueueResponse"),
        "400": error_response("The closed generation DTO or active prompt configuration is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal lacks exact ai:write scope or the feature is disabled."),
        "404": error_response("The active subject is absent or outside the source catalogue."),
        "409": error_response("An idempotency key exists with a different authorization context."),
        "429": error_response("Generation rate or organization token budget exceeded."),
    },
    operation_id="post_ai_exam_generation",
)


def _usage_read(method: str) -> OperationContract:
    return OperationContract(
        method=method,
        summary="Read organization AI usage",
        description="Requires organization-wide ai:manage and returns per-feature audited token and cost totals.",
        permission="ai:manage",
        security=SESSION_SECURITY,
        parameters=(
            {
                "name": "month",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "pattern": r"^\d{4}-(0[1-9]|1[0-2])$"},
            },
        ),
        responses={
            "200": (
                {"description": "AI usage headers are available; HEAD has no response body."}
                if method == "HEAD"
                else json_response(
                    "Per-feature usage for the selected month.",
                    "AIUsageReportResponse",
                )
            ),
            **_read_errors(),
        },
        operation_id=f"{method.lower()}_ai_usage_report",
    )


AI_USAGE_REPORT_CONTRACTS = (_usage_read("GET"), _usage_read("HEAD"))


def _success_schema(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": data,
        },
        "required": ["success", "data"],
    }


_AI_FEATURE_VALUES = [
    "assignment_feedback",
    "exam_generation",
    "content_summary",
    "placement_generation",
    "form_analysis",
    "writing_marking",
    "material_generation",
    "template_generation",
]
_AI_STATUS_VALUES = ["queued", "running", "succeeded", "failed", "denied_budget", "uncertain"]

_SCOPE_REF = {
    "type": "object",
    "nullable": True,
    "additionalProperties": False,
    "properties": {
        "id": {"type": "integer", "minimum": 1},
        "name": {"type": "string"},
    },
    "required": ["id", "name"],
}

OPENAPI_SCHEMAS: dict[str, dict[str, Any]] = {
    "AIFeature": {"type": "string", "enum": _AI_FEATURE_VALUES},
    "AIRequestStatus": {"type": "string", "enum": _AI_STATUS_VALUES},
    "AIRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "feature": {"$ref": "#/components/schemas/AIFeature"},
            "status": {"$ref": "#/components/schemas/AIRequestStatus"},
            "input_tokens": {"type": "integer", "minimum": 0},
            "output_tokens": {"type": "integer", "minimum": 0},
            "cache_read_tokens": {"type": "integer", "minimum": 0},
            "cache_creation_tokens": {"type": "integer", "minimum": 0},
            "total_tokens": {"type": "integer", "format": "int64", "minimum": 0},
            "cost_microusd": {"type": "integer", "format": "int64", "minimum": 0},
            "created_at": {"type": "string", "format": "date-time"},
            "finished_at": {"type": "string", "format": "date-time", "nullable": True},
            "scope": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"branch": _SCOPE_REF, "department": _SCOPE_REF},
                "required": ["branch", "department"],
            },
            "content_available": {"type": "boolean"},
            "output_text": {
                "type": "string",
                "maxLength": 250000,
                "description": "Detail-only and permission-pruned generated content.",
            },
        },
        "required": [
            "id",
            "feature",
            "status",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "total_tokens",
            "cost_microusd",
            "created_at",
            "finished_at",
            "scope",
            "content_available",
        ],
    },
    "AIRequestResponse": _success_schema({"$ref": "#/components/schemas/AIRequest"}),
    "AIRequestPageResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"type": "array", "items": {"$ref": "#/components/schemas/AIRequest"}},
            "pagination": {"$ref": "#/components/schemas/Pagination"},
        },
        "required": ["success", "data", "pagination"],
    },
    "AIBudget": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "daily_token_limit": {"type": "integer", "minimum": 0},
            "monthly_token_limit": {"type": "integer", "minimum": 0},
            "tokens_used_today": {"type": "integer", "format": "int64", "minimum": 0},
            "tokens_used_month": {"type": "integer", "format": "int64", "minimum": 0},
            "is_enabled": {"type": "boolean"},
        },
        "required": [
            "daily_token_limit",
            "monthly_token_limit",
            "tokens_used_today",
            "tokens_used_month",
            "is_enabled",
        ],
    },
    "AIBudgetResponse": _success_schema({"$ref": "#/components/schemas/AIBudget"}),
    "AIBudgetPatchRequest": {
        "type": "object",
        "additionalProperties": False,
        "minProperties": 1,
        "properties": {
            "daily_token_limit": {"type": "integer", "minimum": 0, "maximum": 2000000000},
            "monthly_token_limit": {"type": "integer", "minimum": 0, "maximum": 2000000000},
            "is_enabled": {"type": "boolean"},
        },
    },
    "AIExamGenerationRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject_id": {"type": "integer", "minimum": 1},
            "exam_type": {"type": "string", "maxLength": 32},
            "question_count": {"type": "integer", "minimum": 1, "maximum": 100},
            "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
        },
        "required": ["subject_id", "exam_type", "question_count", "difficulty"],
    },
    "AIQueueResponse": _success_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"request_id": {"type": "integer", "minimum": 1}},
            "required": ["request_id"],
        }
    ),
    "AIUsageItem": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "feature": {"$ref": "#/components/schemas/AIFeature"},
            "requests": {"type": "integer", "minimum": 0},
            "input_tokens": {"type": "integer", "format": "int64", "minimum": 0},
            "output_tokens": {"type": "integer", "format": "int64", "minimum": 0},
            "cache_read_tokens": {"type": "integer", "format": "int64", "minimum": 0},
            "cache_creation_tokens": {"type": "integer", "format": "int64", "minimum": 0},
            "total_tokens": {"type": "integer", "format": "int64", "minimum": 0},
            "cost_microusd": {"type": "integer", "format": "int64", "minimum": 0},
        },
        "required": [
            "feature",
            "requests",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "total_tokens",
            "cost_microusd",
        ],
    },
    "AIUsageReportResponse": _success_schema(
        {"type": "array", "items": {"$ref": "#/components/schemas/AIUsageItem"}}
    ),
}
