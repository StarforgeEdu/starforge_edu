"""Closed OpenAPI contract for per-student assessment evidence."""

from __future__ import annotations

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)

_SCORE_INPUT = {
    "oneOf": [
        {"type": "number", "minimum": 0, "maximum": 9999.99},
        {"type": "string", "pattern": r"^\d{1,4}(?:\.\d{1,2})?$"},
    ]
}
_POSITIVE_SCORE_INPUT = {
    "oneOf": [
        {"type": "number", "minimum": 0.01, "maximum": 9999.99},
        {
            "type": "string",
            "pattern": r"^(?=.*[1-9])\d{1,4}(?:\.\d{1,2})?$",
        },
    ]
}


def _success(data: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"success": {"type": "boolean", "enum": [True]}, "data": data},
        "required": ["success", "data"],
    }


def _result_entry(identity: str) -> dict:
    identity_schema = (
        {"type": "integer", "minimum": 1}
        if identity == "student"
        else {"type": "string", "minLength": 1, "maxLength": 32}
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            identity: identity_schema,
            "score": _SCORE_INPUT,
            "note": {"type": "string", "maxLength": 255, "default": ""},
            "components": {
                "type": "array",
                "maxItems": 20,
                "description": "Omit to preserve an existing breakdown; send [] to clear it.",
                "items": {"$ref": "#/components/schemas/ExamResultComponentInput"},
            },
        },
        "required": [identity, "score"],
    }


OPENAPI_SCHEMAS = {
    "ExamResultComponentInput": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 64},
            "score": _SCORE_INPUT,
            "max_score": _POSITIVE_SCORE_INPUT,
        },
        "required": ["name", "score", "max_score"],
    },
    "ExamResultComponent": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 64},
            "score": {"type": "string", "pattern": r"^\d{1,4}(?:\.\d{1,2})?$"},
            "max_score": {
                "type": "string",
                "pattern": r"^(?=.*[1-9])\d{1,4}(?:\.\d{1,2})?$",
            },
        },
        "required": ["name", "score", "max_score"],
    },
    "ExamResultWriteEntry": {
        "oneOf": [_result_entry("student"), _result_entry("student_code")],
    },
    "ExamResultWriteRequest": {
        "type": "array",
        "maxItems": 5000,
        "items": {"$ref": "#/components/schemas/ExamResultWriteEntry"},
    },
    "ExamResult": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "exam": {"type": "integer", "minimum": 1},
            "student": {"type": "integer", "minimum": 1},
            "student_code": {"type": "string"},
            "student_name": {"type": "string"},
            "score": {"type": "string"},
            "note": {"type": "string", "maxLength": 255},
            "components": {
                "type": "array",
                "maxItems": 20,
                "items": {"$ref": "#/components/schemas/ExamResultComponent"},
            },
            "graded_by": {"type": "integer", "minimum": 1, "nullable": True},
            "graded_at": {"type": "string", "format": "date-time", "nullable": True},
        },
        "required": [
            "id",
            "exam",
            "student",
            "student_code",
            "student_name",
            "score",
            "note",
            "components",
            "graded_by",
            "graded_at",
        ],
    },
    "ExamResultPageResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"type": "array", "items": {"$ref": "#/components/schemas/ExamResult"}},
            "pagination": {"$ref": "#/components/schemas/Pagination"},
        },
        "required": ["success", "data", "pagination"],
    },
    "ExamResultWriteData": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "created": {"type": "integer", "minimum": 0},
            "updated": {"type": "integer", "minimum": 0},
            "results": {"type": "array", "items": {"$ref": "#/components/schemas/ExamResult"}},
        },
        "required": ["created", "updated", "results"],
    },
    "ExamResultWriteResponse": _success({"$ref": "#/components/schemas/ExamResultWriteData"}),
}

_PATH_PARAMETER = (
    {
        "name": "pk",
        "in": "path",
        "required": True,
        "schema": {"type": "integer", "minimum": 1},
    },
)
_PAGE_PARAMETERS = (
    *_PATH_PARAMETER,
    {"name": "page", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1}},
    {
        "name": "page_size",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)


def _results_read(method: str) -> OperationContract:
    return OperationContract(
        method=method,
        summary="List scoped exam results" if method == "GET" else "Check scoped exam results",
        description=(
            "Raw student evidence, including optional authoritative skill components, is limited "
            "to principals with academics:write for this exact exam cohort."
        ),
        permission="academics:write",
        security=SESSION_SECURITY,
        parameters=_PAGE_PARAMETERS,
        responses={
            "200": json_response("Scoped result page.", "ExamResultPageResponse"),
            "400": error_response("Pagination is invalid."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The principal lacks academics write authority."),
            "404": error_response("The exam is outside the caller's exact scope."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id=f"{method.lower()}_academics_exam_results",
    )


EXAM_RESULTS_CONTRACTS = (
    _results_read("GET"),
    _results_read("HEAD"),
    OperationContract(
        method="POST",
        summary="Record bounded overall and per-skill exam evidence",
        description=(
            "Upserts one result per enrolled student in a draft exam. Component names are "
            "normalized and unique; all scores are finite, nonnegative, precision-bounded, and "
            "not above their declared maximum. Published evidence requires correction workflow."
        ),
        permission="academics:write",
        security=UNSAFE_SESSION_SECURITY,
        parameters=_PATH_PARAMETER,
        request_body=json_request("ExamResultWriteRequest"),
        responses={
            "200": json_response("Result evidence recorded.", "ExamResultWriteResponse"),
            "400": error_response("The closed result DTO is malformed."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The principal lacks write authority for this cohort."),
            "404": error_response("The exam is outside the caller's exact scope."),
            "409": error_response("Published or correction-pending evidence is locked."),
            "422": error_response("A student, total score, or component is invalid."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id="post_academics_exam_results",
    ),
)
