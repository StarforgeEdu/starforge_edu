"""Closed public contract for teacher content uploads."""

from __future__ import annotations

from core.openapi_contracts import UNSAFE_SESSION_SECURITY, OperationContract, error_response

_LOCATION = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"lesson": {"type": "integer", "minimum": 1}},
            "required": ["lesson"],
        },
        {
            "type": "object",
            "properties": {"folder": {"type": "integer", "minimum": 1}},
            "required": ["folder"],
        },
    ]
}

_UPLOAD_REQUEST = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                **_LOCATION,
                "properties": {
                    "filename": {"type": "string", "minLength": 1, "maxLength": 255},
                    "content_type": {"type": "string", "minLength": 1, "maxLength": 127},
                    "size_bytes": {"type": "integer", "minimum": 1},
                    "title": {"type": "string", "maxLength": 255},
                    "lesson": {"type": "integer", "minimum": 1},
                    "folder": {"type": "integer", "minimum": 1},
                    "audience": {
                        "type": "string",
                        "enum": ["own_students", "global"],
                        "description": (
                            "Optional teacher-only audience assertion. own_students requires a "
                            "cohort library the active teacher actually teaches; global requires "
                            "an active tenant-wide library. Omit for the legacy scoped-library flow."
                        ),
                    },
                    "is_downloadable": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Requested learner download policy. The distinct manager publisher may "
                            "retain or override it before publication."
                        ),
                    },
                },
                "required": ["filename", "content_type", "size_bytes"],
            }
        }
    },
}

_UPLOAD_RESPONSE = {
    "description": "Owner-bound, exact-size signed upload instructions for a private draft.",
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
                            "file_id": {"type": "integer", "minimum": 1},
                            "url": {"type": "string", "format": "uri"},
                            "key": {"type": "string"},
                            "expires_in": {"type": "integer", "minimum": 1},
                        },
                        "required": ["file_id", "url", "key", "expires_in"],
                    },
                },
                "required": ["success", "data"],
            }
        }
    },
}


CONTENT_UPLOAD_URL_OPERATIONS = (
    OperationContract(
        method="POST",
        summary="Prepare a secure library upload",
        description=(
            "Creates an unapproved private draft and an exact-size signed upload. Explicit "
            "teacher audiences are revalidated from the active role principal; global drafts "
            "remain hidden from learners until the existing distinct publisher approval."
        ),
        security=UNSAFE_SESSION_SECURITY,
        permission="content:write",
        request_body=_UPLOAD_REQUEST,
        responses={
            "200": _UPLOAD_RESPONSE,
            "400": error_response("The closed upload DTO or target location is invalid."),
            "401": error_response("The session is absent, invalid, expired, or revoked."),
            "403": error_response("The active account cannot author this audience."),
            "422": error_response("The file type, size, quota, or location cannot be accepted."),
            "429": error_response("Authenticated request rate limit exceeded."),
            "503": error_response("Object storage or feature policy is temporarily unavailable."),
        },
        operation_id="post_content_secure_upload",
    ),
)
