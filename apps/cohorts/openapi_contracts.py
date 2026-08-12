"""Explicit mobile contract for derived cohort lesson-cycle progress."""

from __future__ import annotations

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
)

_NEXT_LESSON = {
    "type": "object",
    "nullable": True,
    "additionalProperties": False,
    "properties": {
        "id": {"type": "integer", "format": "int64", "minimum": 1},
        "title": {"type": "string"},
        "starts_at": {"type": "string", "format": "date-time"},
        "ends_at": {"type": "string", "format": "date-time"},
        "room": {"type": "integer", "format": "int64", "minimum": 1, "nullable": True},
        "room_name": {"type": "string", "nullable": True},
        "teacher": {"type": "integer", "format": "int64", "minimum": 1},
        "teacher_name": {"type": "string"},
        "cycle_lesson_number": {"type": "integer", "minimum": 1, "maximum": 12},
        "is_cycle_exam_day": {"type": "boolean"},
    },
    "required": [
        "id",
        "title",
        "starts_at",
        "ends_at",
        "room",
        "room_name",
        "teacher",
        "teacher_name",
        "cycle_lesson_number",
        "is_cycle_exam_day",
    ],
}

_PROGRESS_DATA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cohort": {"type": "integer", "format": "int64", "minimum": 1},
        "current_level": {"type": "string"},
        "current_study_month": {"type": "integer", "minimum": 1, "maximum": 600},
        "lesson_cycle_length": {"type": "integer", "enum": [8, 12]},
        "completed_lessons": {"type": "integer", "minimum": 0},
        "completed_cycles": {"type": "integer", "minimum": 0},
        "completed_in_current_cycle": {"type": "integer", "minimum": 0, "maximum": 11},
        "next_cycle_lesson_number": {"type": "integer", "minimum": 1, "maximum": 12},
        "lessons_remaining_in_cycle": {"type": "integer", "minimum": 1, "maximum": 12},
        "exam_day_due": {"type": "boolean"},
        "exam_reminder_due": {"type": "boolean"},
        "exam_reminder_window_days": {"type": "integer", "enum": [7]},
        "next_scheduled_lesson": _NEXT_LESSON,
        "past_scheduled_lessons_without_completion": {"type": "integer", "minimum": 0},
        "completion_data_complete": {"type": "boolean"},
        "level_progression_mode": {"type": "string", "enum": ["manual"]},
        "automatic_level_progression": {"type": "boolean", "enum": [False]},
    },
    "required": [
        "cohort",
        "current_level",
        "current_study_month",
        "lesson_cycle_length",
        "completed_lessons",
        "completed_cycles",
        "completed_in_current_cycle",
        "next_cycle_lesson_number",
        "lessons_remaining_in_cycle",
        "exam_day_due",
        "exam_reminder_due",
        "exam_reminder_window_days",
        "next_scheduled_lesson",
        "past_scheduled_lessons_without_completion",
        "completion_data_complete",
        "level_progression_mode",
        "automatic_level_progression",
    ],
}

_SUCCESS = {
    "description": "Cycle progress derived only from explicitly completed lessons.",
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": _PROGRESS_DATA,
                    "warnings": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/RuntimeWarning"},
                    },
                },
                "required": ["success", "data"],
            }
        }
    },
}

CYCLE_PROGRESS_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Read a cohort's derived lesson-cycle and exam-day state",
    description=(
        "Supports an 8- or 12-lesson cohort cadence. Only explicitly COMPLETED lessons "
        "advance progress; overdue SCHEDULED lessons are disclosed as incomplete evidence. "
        "The last slot is signaled as the exam day and an exam reminder becomes due seven "
        "days before its next scheduled occurrence. This operation never changes level text."
    ),
    security=SESSION_SECURITY,
    permission="cohorts:read",
    parameters=(
        {
            "name": "pk",
            "in": "path",
            "required": True,
            "schema": {"type": "integer", "format": "int64", "minimum": 1},
        },
    ),
    responses={
        "200": _SUCCESS,
        "401": error_response("The role session is absent, invalid, expired, or revoked."),
        "403": error_response("The active principal cannot read this cohort."),
        "404": error_response("The cohort does not exist."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_cohort_cycle_progress",
)


_TEACHING_PROGRESS_REQUEST = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "minProperties": 1,
                "properties": {
                    "level": {"type": "string", "maxLength": 64},
                    "study_month": {"type": "integer", "minimum": 1, "maximum": 600},
                    "lesson_cycle_length": {"type": "integer", "enum": [8, 12]},
                },
            }
        }
    },
}

_TEACHING_PROGRESS_SUCCESS = {
    "description": "The primary teacher's bounded curriculum metadata was updated.",
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "cohort": {"type": "integer", "format": "int64", "minimum": 1},
                            "level": {"type": "string"},
                            "study_month": {"type": "integer", "minimum": 1, "maximum": 600},
                            "lesson_cycle_length": {"type": "integer", "enum": [8, 12]},
                            "updated_at": {"type": "string", "format": "date-time"},
                        },
                        "required": [
                            "cohort",
                            "level",
                            "study_month",
                            "lesson_cycle_length",
                            "updated_at",
                        ],
                    },
                },
                "required": ["success", "data"],
            }
        }
    },
}

TEACHING_PROGRESS_PATCH_CONTRACT = OperationContract(
    method="PATCH",
    summary="Update the primary teacher's cohort curriculum progress",
    description=(
        "Updates only level text, the explicit study month, and the supported 8/12 lesson "
        "cadence. The active role principal must be the cohort's exact primary teacher; "
        "assistants and co-teachers cannot mutate these fields."
    ),
    security=UNSAFE_SESSION_SECURITY,
    permission="academics:write",
    request_body=_TEACHING_PROGRESS_REQUEST,
    parameters=(
        {
            "name": "pk",
            "in": "path",
            "required": True,
            "schema": {"type": "integer", "format": "int64", "minimum": 1},
        },
    ),
    responses={
        "200": _TEACHING_PROGRESS_SUCCESS,
        "400": error_response("A field is missing, unsupported, or invalid."),
        "401": error_response("The role session is absent, invalid, expired, or revoked."),
        "403": error_response("The active principal is not this cohort's primary teacher."),
        "404": error_response("The cohort does not exist or is outside the permitted scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="patch_cohort_teaching_progress",
)
