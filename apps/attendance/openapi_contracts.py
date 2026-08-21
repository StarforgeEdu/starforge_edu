"""Closed OpenAPI contracts for attendance history and lesson marking."""

from __future__ import annotations

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)

_CARD_TYPE = {"type": "string", "enum": ["", "smart", "warning"]}
_STATUS = {"type": "string", "enum": ["present", "absent", "late", "excused"]}


def _success(data: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": data,
        },
        "required": ["success", "data"],
    }


OPENAPI_SCHEMAS = {
    "AttendanceRecord": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "student": {"type": "integer", "minimum": 1},
            "student_name": {"type": "string"},
            "lesson": {"type": "integer", "minimum": 1},
            "lesson_title": {"type": "string"},
            "lesson_starts_at": {"type": "string", "format": "date-time"},
            "cohort": {"type": "integer", "minimum": 1},
            "cohort_name": {"type": "string"},
            "teacher": {"type": "integer", "minimum": 1},
            "teacher_name": {"type": "string"},
            "status": _STATUS,
            "card_type": _CARD_TYPE,
            "arrived_at": {"type": "string", "format": "date-time", "nullable": True},
            "note": {"type": "string", "maxLength": 500},
            "marked_by": {"type": "integer", "minimum": 1, "nullable": True},
            "marked_at": {"type": "string", "format": "date-time", "nullable": True},
            "auto_marked": {"type": "boolean"},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "required": [
            "id",
            "student",
            "student_name",
            "lesson",
            "lesson_title",
            "lesson_starts_at",
            "cohort",
            "cohort_name",
            "teacher",
            "teacher_name",
            "status",
            "card_type",
            "arrived_at",
            "note",
            "marked_by",
            "marked_at",
            "auto_marked",
            "created_at",
        ],
    },
    "AttendanceMarkEntry": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "student": {"type": "integer", "minimum": 1},
            "status": _STATUS,
            "arrived_at": {"type": "string", "format": "date-time", "nullable": True},
            "note": {"type": "string", "maxLength": 500, "default": ""},
            "card_type": {
                **_CARD_TYPE,
                "description": ("Omit to preserve an existing card; send an empty string to clear it."),
            },
        },
        "required": ["student", "status"],
    },
    "AttendanceMarkRequest": {
        "type": "array",
        "maxItems": 5000,
        "items": {"$ref": "#/components/schemas/AttendanceMarkEntry"},
    },
    "AttendanceRecordResponse": _success({"$ref": "#/components/schemas/AttendanceRecord"}),
    "AttendanceRecordPageResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/AttendanceRecord"},
            },
            "pagination": {"$ref": "#/components/schemas/Pagination"},
        },
        "required": ["success", "data", "pagination"],
    },
    "AttendanceMarkData": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "created": {"type": "integer", "minimum": 0},
            "updated": {"type": "integer", "minimum": 0},
            "records": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/AttendanceRecord"},
            },
        },
        "required": ["created", "updated", "records"],
    },
    "AttendanceMarkResponse": _success({"$ref": "#/components/schemas/AttendanceMarkData"}),
}

_RECORD_FILTERS = (
    *(
        {
            "name": name,
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1},
        }
        for name in ("student", "lesson", "cohort")
    ),
    {"name": "status", "in": "query", "required": False, "schema": _STATUS},
    *(
        {
            "name": name,
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date-time"},
        }
        for name in ("date_from", "date_to")
    ),
    {
        "name": "ordering",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": ["created_at", "-created_at", "marked_at", "-marked_at"],
        },
    },
    {"name": "page", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1}},
    {
        "name": "page_size",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)


def _records_read(method: str) -> OperationContract:
    return OperationContract(
        method=method,
        summary="List scoped attendance evidence" if method == "GET" else "Check scoped attendance evidence",
        description=(
            "Returns only records visible through the caller's exact attendance membership scope. "
            "card_type is distinct from card-scan provenance in note."
        ),
        permission="attendance:read",
        security=SESSION_SECURITY,
        parameters=_RECORD_FILTERS,
        responses={
            "200": json_response("Scoped attendance record page.", "AttendanceRecordPageResponse"),
            "400": error_response("A filter or pagination value is invalid."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The principal lacks attendance read authority."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id=f"{method.lower()}_attendance_records",
    )


RECORDS_COLLECTION_CONTRACTS = (_records_read("GET"), _records_read("HEAD"))

MARK_CONTRACT = OperationContract(
    method="POST",
    summary="Mark a lesson register and optionally issue feedback cards",
    description=(
        "Upserts one row per enrolled student after lesson-start, enforcing exact lesson/cohort "
        "scope and the configured correction window. card_type is a closed enum; omission "
        "preserves existing evidence and an empty string explicitly clears it."
    ),
    permission="attendance:write",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("AttendanceMarkRequest"),
    responses={
        "200": json_response("Attendance register saved.", "AttendanceMarkResponse"),
        "400": error_response("The closed mark DTO is malformed."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("Lesson scope or correction-window authority failed."),
        "404": error_response("The lesson is outside the caller's scoped workspace."),
        "422": error_response("The lesson state, roster, or card type is invalid."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_attendance_lesson_mark",
)
