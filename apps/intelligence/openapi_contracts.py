"""Explicit OpenAPI metadata for the permission-pruned executive snapshot."""

from core.openapi_contracts import (
    SESSION_SECURITY,
    OperationContract,
    error_response,
    json_response,
)

EXECUTIVE_QUERY_PARAMETERS = (
    {
        "name": "branch",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1},
        "description": "A positive branch ID inside the caller's exact intelligence scope.",
    },
    {
        "name": "department",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1},
        "description": "A positive department ID inside the selected/authorized branch.",
    },
    {
        "name": "date_from",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "format": "date"},
        "description": (
            "Inclusive organization-timezone start date. If omitted, the service uses the "
            "29 days before date_to (or the trailing 30-day window when both dates are omitted)."
        ),
    },
    {
        "name": "date_to",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "format": "date"},
        "description": (
            "Inclusive organization-timezone end date; defaults to today. The inclusive window "
            "cannot exceed 366 days and 9999-12-31 is rejected."
        ),
    },
    {
        "name": "If-None-Match",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
        "description": "Previously returned ETag for private conditional revalidation.",
    },
)

_EXECUTIVE_RESPONSE_HEADERS = {
    "ETag": {
        "schema": {"type": "string"},
        "description": "Private validator for this principal, scope, locale, and date window.",
    },
    "Cache-Control": {
        "schema": {"type": "string"},
        "description": "Always requires private revalidation before reuse.",
    },
    "Vary": {
        "schema": {"type": "string"},
        "description": "Includes Accept-Language and Authorization.",
    },
}

EXECUTIVE_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Get a permission-pruned executive snapshot",
    description=(
        "Student, retention, capacity, risk, attendance, teacher-evidence, finance, and actionable "
        "attention sections share one generated_at, exact authorized scope, locale, currency, and "
        "inclusive date window. Personal counts use the exact role-native principal. Optional "
        "domains are omitted when permission or immutable scope cannot be proven; coverage and "
        "structured warnings explain every omission. Unknown, duplicate, blank, malformed, "
        "reversed, and over-broad query values fail with field-scoped 400 responses."
    ),
    permission="intelligence:read",
    security=SESSION_SECURITY,
    parameters=EXECUTIVE_QUERY_PARAMETERS,
    responses={
        "200": {
            **json_response("Scoped executive snapshot.", "ExecutiveSummaryResponse"),
            "headers": _EXECUTIVE_RESPONSE_HEADERS,
        },
        "304": {
            **json_response("The scoped snapshot has not changed."),
            "headers": _EXECUTIVE_RESPONSE_HEADERS,
        },
        "400": error_response(
            "A query name/value/window is invalid or a requested selector is out of scope."
        ),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal has no active intelligence scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_intelligence_executive_summary",
)

EXECUTIVE_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Revalidate a permission-pruned executive snapshot",
    description="Same validation, authorization, ETag, and status semantics as GET, without a body.",
    permission="intelligence:read",
    security=SESSION_SECURITY,
    parameters=EXECUTIVE_QUERY_PARAMETERS,
    responses={
        "200": {
            **json_response("Scoped snapshot metadata."),
            "headers": _EXECUTIVE_RESPONSE_HEADERS,
        },
        "304": {
            **json_response("The scoped snapshot has not changed."),
            "headers": _EXECUTIVE_RESPONSE_HEADERS,
        },
        "400": error_response(
            "A query name/value/window is invalid or a requested selector is out of scope."
        ),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal has no active intelligence scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_intelligence_executive_summary",
)
