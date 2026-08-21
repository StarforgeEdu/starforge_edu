"""Executable OpenAPI contracts for scoped staff meetings and RSVP actions."""

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


def _page_parameters(*extra: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return (
        *extra,
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
    )


_MEETING_FILTERS = _page_parameters(
    {
        "name": "status",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["scheduled", "cancelled"]},
    },
    {
        "name": "branch",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1},
    },
    {
        "name": "ordering",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["starts_at", "-starts_at"]},
    },
)


def _meeting_read(method: str, *, collection: bool = False, upcoming: bool = False) -> OperationContract:
    if upcoming:
        summary = "List the current principal's upcoming invitations"
        parameters = _page_parameters()
    elif collection:
        summary = "List visible staff meetings"
        parameters = _MEETING_FILTERS
    else:
        summary = "Read a visible staff meeting"
        parameters = ()
    responses = {
        "200": (
            json_response(
                "Visible meeting page.",
                "MeetingPageResponse",
            )
            if method == "GET" and (collection or upcoming)
            else json_response("Visible meeting.", "MeetingResponse")
            if method == "GET"
            else json_response("Meeting visibility confirmed.")
        ),
        "400": error_response("A declared query parameter is malformed or unsupported."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The authenticated account is not an active staff or teacher principal."),
        "429": error_response("Authenticated request rate limit exceeded."),
    }
    if not (collection or upcoming):
        responses["404"] = error_response("The meeting is not visible to the current principal.")
    return OperationContract(
        method=method,
        summary=summary,
        description=(
            "Visibility is derived from the exact active staff or teacher principal: managers "
            "receive their authorized branch union and invitees receive only their own invites. "
            "A non-manager response omits other attendees, creator identity, and cancellation "
            "identity. Out-of-scope IDs return not found."
        ),
        security=SESSION_SECURITY,
        parameters=parameters,
        responses=responses,
        operation_id=(
            f"{method.lower()}_meetings_upcoming"
            if upcoming
            else f"{method.lower()}_meetings_{'collection' if collection else 'detail'}"
        ),
    )


MEETINGS_COLLECTION_CONTRACTS = (
    _meeting_read("GET", collection=True),
    _meeting_read("HEAD", collection=True),
    OperationContract(
        method="POST",
        summary="Schedule a scoped staff meeting",
        description=(
            "Schedules a meeting lasting no more than 24 hours with 1–200 unique active staff "
            "or teacher principals in the selected branch. Use either legacy attendee user IDs "
            "or explicit role-account invitees, never both. A scoped manager cannot probe or "
            "schedule another branch, and an ambiguous bridge user is never guessed."
        ),
        permission="meeting:write",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request("MeetingCreateRequest"),
        responses={
            "201": json_response("Meeting scheduled.", "MeetingResponse"),
            "400": error_response("The closed request DTO, time window, branch, or invitees are invalid."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The branch is outside the caller's meeting write scope."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id="post_meetings_collection",
    ),
)

MEETING_DETAIL_CONTRACTS = (
    _meeting_read("GET"),
    _meeting_read("HEAD"),
)

MEETING_UPCOMING_CONTRACTS = (
    _meeting_read("GET", upcoming=True),
    _meeting_read("HEAD", upcoming=True),
)

MEETING_CANCEL_CONTRACT = OperationContract(
    method="POST",
    summary="Cancel a scoped staff meeting",
    description=(
        "Idempotently cancels a meeting within the exact branch-wide meeting:write scope. "
        "Department-only grants fail closed because meetings do not yet store department "
        "ownership. The optional JSON body must be empty."
    ),
    permission="meeting:write",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("EmptyWorkflowRequest", required=False),
    responses={
        "200": json_response("Meeting cancelled or already cancelled.", "MeetingResponse"),
        "400": error_response("The optional body is malformed or contains fields."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks branch-wide meeting write authority."),
        "404": error_response("The meeting is outside the caller's visible scope."),
        "422": error_response("The meeting cannot be cancelled from its current state."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_meetings_cancel",
)

MEETING_RESPOND_CONTRACT = OperationContract(
    method="POST",
    summary="Accept or decline the current principal's invitation",
    description=(
        "Updates only the invitation matching the exact current staff or teacher principal. "
        "Repeating the same response is idempotent; a different role account sharing the same "
        "bridge user cannot read or mutate the invitation."
    ),
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("MeetingRespondRequest"),
    responses={
        "200": json_response("Invitation response recorded.", "MeetingResponse"),
        "400": error_response("The closed request DTO or response value is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session is read-only or is not a staff/teacher principal."),
        "404": error_response("No invitation exists for the exact current principal."),
        "422": error_response("The meeting is no longer open for responses."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_meetings_respond",
)


def _success_schema(data_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": data_schema,
        },
        "required": ["success", "data"],
    }


OPENAPI_SCHEMAS: dict[str, dict[str, Any]] = {
    "MeetingCreateRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "agenda": {"type": "string", "maxLength": 20_000},
            "location": {"type": "string", "maxLength": 200},
            "starts_at": {"type": "string", "format": "date-time"},
            "ends_at": {
                "type": "string",
                "format": "date-time",
                "description": "Must be after starts_at and at most 24 hours later.",
            },
            "branch": {"type": "integer", "format": "int64", "minimum": 1, "nullable": True},
            "attendees": {
                "type": "array",
                "minItems": 1,
                "maxItems": 200,
                "uniqueItems": True,
                "items": {"type": "integer", "format": "int64", "minimum": 1},
            },
            "invitees": {
                "type": "array",
                "minItems": 1,
                "maxItems": 200,
                "uniqueItems": True,
                "items": {"$ref": "#/components/schemas/MeetingPrincipalSelector"},
            },
        },
        "required": ["title", "starts_at", "ends_at"],
        "oneOf": [{"required": ["attendees"]}, {"required": ["invitees"]}],
        "not": {"required": ["attendees", "invitees"]},
    },
    "MeetingRespondRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "response": {"type": "string", "enum": ["accepted", "declined"]},
        },
        "required": ["response"],
    },
    "MeetingPrincipalSelector": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["staff", "teacher"]},
            "id": {"type": "integer", "format": "int64", "minimum": 1},
        },
        "required": ["kind", "id"],
    },
    "MeetingPrincipal": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["staff", "teacher"]},
            "id": {"type": "integer", "format": "int64", "minimum": 1},
            "display_name": {"type": "string", "nullable": True},
            "account_label": {"type": "string", "enum": ["Staff", "Teacher"]},
        },
        "required": ["kind", "id", "display_name", "account_label"],
    },
    "MeetingAttendee": {
        "type": "object",
        "additionalProperties": False,
        "description": "Identity properties are present only for an authorized meeting manager.",
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "response": {"type": "string", "enum": ["invited", "accepted", "declined"]},
            "responded_at": {"type": "string", "format": "date-time", "nullable": True},
            "principal": {
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/MeetingPrincipal"}],
            },
        },
        "required": ["id", "response", "responded_at"],
    },
    "Meeting": {
        "type": "object",
        "additionalProperties": False,
        "description": "Management-only identity fields are omitted for ordinary invitees.",
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "title": {"type": "string"},
            "agenda": {"type": "string"},
            "branch": {"type": "integer", "format": "int64", "nullable": True},
            "branch_name": {"type": "string", "nullable": True},
            "starts_at": {"type": "string", "format": "date-time"},
            "ends_at": {"type": "string", "format": "date-time"},
            "location": {"type": "string"},
            "status": {"type": "string", "enum": ["scheduled", "cancelled"]},
            "attendee_count": {"type": "integer", "minimum": 0, "maximum": 200},
            "attendees": {
                "type": "array",
                "maxItems": 200,
                "items": {"$ref": "#/components/schemas/MeetingAttendee"},
            },
            "cancelled_at": {"type": "string", "format": "date-time", "nullable": True},
            "created_at": {"type": "string", "format": "date-time"},
            "created_by": {
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/MeetingPrincipal"}],
            },
            "created_by_attribution_status": {
                "type": "string",
                "enum": ["captured", "resolved", "quarantined"],
            },
            "cancelled_by": {
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/MeetingPrincipal"}],
            },
            "cancelled_by_attribution_status": {
                "type": "string",
                "enum": ["captured", "resolved", "quarantined", "not_applicable"],
            },
            "unresolved_attendee_count": {"type": "integer", "minimum": 0},
        },
        "required": [
            "id",
            "title",
            "agenda",
            "branch",
            "branch_name",
            "starts_at",
            "ends_at",
            "location",
            "status",
            "attendee_count",
            "attendees",
            "cancelled_at",
            "created_at",
        ],
    },
    "MeetingResponse": _success_schema({"$ref": "#/components/schemas/Meeting"}),
    "MeetingPageResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/Meeting"},
            },
            "pagination": {"$ref": "#/components/schemas/Pagination"},
        },
        "required": ["success", "data", "pagination"],
    },
}
