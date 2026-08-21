"""Executable HTTP contracts for messaging realtime recovery and read state."""

from __future__ import annotations

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
)

_PRINCIPAL_KIND = {"type": "string", "enum": ["student", "teacher", "parent", "staff"]}
_POSITIVE_ID = {"type": "integer", "format": "int64", "minimum": 1}
_CURSOR = {"type": "integer", "format": "int64", "minimum": 0}

_EVENT_KINDS = [
    "message.created",
    "message.updated",
    "message.deleted",
    "reaction.added",
    "reaction.removed",
    "read.updated",
]

_EVENT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "thread_id": _POSITIVE_ID,
        "sequence": {"type": "integer", "format": "int64", "minimum": 1},
        "kind": {"type": "string", "enum": _EVENT_KINDS},
        "message_id": _POSITIVE_ID,
        "actor_principal_kind": _PRINCIPAL_KIND,
        "actor_principal_id": _POSITIVE_ID,
        "created_at": {"type": "string", "format": "date-time"},
    },
    "required": [
        "thread_id",
        "sequence",
        "kind",
        "message_id",
        "actor_principal_kind",
        "actor_principal_id",
        "created_at",
    ],
}

_REACTION = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "emoji": {"type": "string", "minLength": 1, "maxLength": 16},
        "count": {"type": "integer", "minimum": 1},
        "reacted_by_me": {"type": "boolean"},
    },
    "required": ["emoji", "count", "reacted_by_me"],
}

_MESSAGE_DATA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": _POSITIVE_ID,
        "thread": _POSITIVE_ID,
        "sender": {**_POSITIVE_ID, "nullable": True},
        "sender_principal_kind": {**_PRINCIPAL_KIND, "nullable": True},
        "sender_principal_id": {**_POSITIVE_ID, "nullable": True},
        "sender_attribution_status": {
            "type": "string",
            "enum": ["captured", "resolved", "unresolved", "conflicting", "quarantined"],
        },
        "body": {"type": "string", "maxLength": 10000},
        "attachments": {"type": "array", "items": {"type": "string", "maxLength": 512}},
        "version": {"type": "integer", "minimum": 1},
        "edited_at": {"type": "string", "format": "date-time", "nullable": True},
        "deleted_at": {"type": "string", "format": "date-time", "nullable": True},
        "is_deleted": {"type": "boolean"},
        "reactions": {"type": "array", "items": _REACTION},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "required": [
        "id",
        "thread",
        "sender",
        "sender_principal_kind",
        "sender_principal_id",
        "sender_attribution_status",
        "body",
        "attachments",
        "version",
        "edited_at",
        "deleted_at",
        "is_deleted",
        "reactions",
        "created_at",
    ],
}

_MESSAGE_ID_PARAMETER = {
    "name": "pk",
    "in": "path",
    "required": True,
    "schema": _POSITIVE_ID,
    "description": "Participant-scoped message identifier.",
}

_EMOJI_PARAMETER = {
    "name": "emoji",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 16},
    "description": "URL-encoded emoji previously added by this exact principal.",
}

_EVENT_PAGE_DATA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "thread_id": _POSITIVE_ID,
        "events": {"type": "array", "items": _EVENT, "maxItems": 100},
        "requested_after": _CURSOR,
        "next_cursor": _CURSOR,
        "high_watermark": _CURSOR,
        "recovery_floor": {"type": "integer", "format": "int64", "minimum": 1},
        "has_more": {"type": "boolean"},
        "reset_required": {"type": "boolean"},
        "generated_at": {"type": "string", "format": "date-time"},
    },
    "required": [
        "thread_id",
        "events",
        "requested_after",
        "next_cursor",
        "high_watermark",
        "recovery_floor",
        "has_more",
        "reset_required",
        "generated_at",
    ],
}

_READ_STATE_DATA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "thread_id": _POSITIVE_ID,
        "changed": {"type": "boolean"},
        "through_message_id": {**_POSITIVE_ID, "nullable": True},
        "read_at": {"type": "string", "format": "date-time", "nullable": True},
        "event_cursor": {
            "type": "integer",
            "format": "int64",
            "minimum": 1,
            "nullable": True,
        },
    },
    "required": ["thread_id", "changed", "through_message_id", "read_at", "event_cursor"],
}


def _success_response(description: str, data_schema: dict) -> dict:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "success": {"type": "boolean", "enum": [True]},
                        "data": data_schema,
                    },
                    "required": ["success", "data"],
                }
            }
        },
    }


_EVENT_PARAMETERS = (
    {
        "name": "after",
        "in": "query",
        "required": False,
        "description": "Last processed sequence for this exact thread; zero starts at the recovery floor.",
        "schema": {**_CURSOR, "default": 0},
    },
    {
        "name": "limit",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
    },
)

THREAD_EVENTS_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Recover ordered messaging thread events",
    description=(
        "Participant-scoped, pointer-only cursor recovery for the matching WebSocket stream. "
        "Live hints are best-effort; this durable page is sequence-ordered and replayable, "
        "so clients recover gaps and deduplicate by sequence. When "
        "reset_required is true, refetch thread/messages and resume from high_watermark."
    ),
    security=SESSION_SECURITY,
    permission="messaging:read",
    parameters=_EVENT_PARAMETERS,
    responses={
        "200": _success_response("Ordered thread event recovery page.", _EVENT_PAGE_DATA),
        "400": error_response("The cursor, limit, or query parameters are invalid."),
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The active principal lacks messaging read authority."),
        "404": error_response("The thread is absent or outside the exact participant principal."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_messaging_thread_events",
)

THREAD_EVENTS_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check messaging thread event recovery access",
    description="Same authorization, cursor validation, and status semantics as GET without a body.",
    security=SESSION_SECURITY,
    permission="messaging:read",
    parameters=_EVENT_PARAMETERS,
    responses={
        "200": {"description": "Thread event recovery is available."},
        "400": error_response("The cursor, limit, or query parameters are invalid."),
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The active principal lacks messaging read authority."),
        "404": error_response("The thread is absent or outside the exact participant principal."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_messaging_thread_events",
)

THREAD_READ_POST_CONTRACT = OperationContract(
    method="POST",
    summary="Advance the current principal's thread read cursor",
    description=(
        "Monotonically advances through one inclusive message. Omitting through_message_id keeps "
        "legacy behavior and snapshots the current final message under the thread lock. Repeating "
        "or submitting an older cursor is idempotent and does not emit another realtime event."
    ),
    security=UNSAFE_SESSION_SECURITY,
    permission="messaging:read",
    request_body={
        "required": False,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"through_message_id": _POSITIVE_ID},
                }
            }
        },
    },
    responses={
        "200": _success_response("Committed inclusive read state.", _READ_STATE_DATA),
        "400": error_response("The read-state body is invalid."),
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The session is read-only or lacks messaging read authority."),
        "404": error_response("The thread/message is absent or outside the exact participant principal."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_messaging_thread_read_state",
)


MESSAGE_PATCH_CONTRACT = OperationContract(
    method="PATCH",
    summary="Edit an authored message",
    description=(
        "Edits only the body of a non-deleted message authored by the exact active principal. "
        "The previous body is retained in an immutable revision and a durable message.updated "
        "pointer is appended. expected_version is optional optimistic-concurrency protection."
    ),
    security=UNSAFE_SESSION_SECURITY,
    permission="messaging:write",
    parameters=(_MESSAGE_ID_PARAMETER,),
    request_body={
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "body": {"type": "string", "minLength": 1, "maxLength": 10000},
                        "expected_version": {"type": "integer", "minimum": 1},
                    },
                    "required": ["body"],
                }
            }
        },
    },
    responses={
        "200": _success_response("Edited message with current reactions.", _MESSAGE_DATA),
        "400": error_response("The body or expected version is invalid."),
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal lacks write authority or does not own the message."),
        "404": error_response("The message is absent or outside the exact participant principal."),
        "409": error_response("The message was deleted or its version changed."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="patch_messaging_message",
)


MESSAGE_DELETE_CONTRACT = OperationContract(
    method="DELETE",
    summary="Soft-delete an authored message",
    description=(
        "Creates an immutable deletion revision and returns a participant-visible tombstone. "
        "Repeating the same author's deletion is idempotent; attachments and prior content remain "
        "private audit evidence and are no longer returned to participants."
    ),
    security=UNSAFE_SESSION_SECURITY,
    permission="messaging:write",
    parameters=(_MESSAGE_ID_PARAMETER,),
    responses={
        "204": {"description": "Message is soft-deleted."},
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal lacks write authority or does not own the message."),
        "404": error_response("The message is absent or outside the exact participant principal."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="delete_messaging_message",
)


MESSAGE_REACTION_POST_CONTRACT = OperationContract(
    method="POST",
    summary="Add a message reaction",
    description=(
        "Adds one Unicode emoji for the exact participant principal. Repeating an active reaction "
        "is idempotent and does not append a duplicate realtime event."
    ),
    security=UNSAFE_SESSION_SECURITY,
    permission="messaging:write",
    parameters=(_MESSAGE_ID_PARAMETER,),
    request_body={
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"emoji": {"type": "string", "minLength": 1, "maxLength": 16}},
                    "required": ["emoji"],
                }
            }
        },
    },
    responses={
        "200": _success_response("Message with current aggregated reactions.", _MESSAGE_DATA),
        "400": error_response("The reaction is missing or is not an emoji."),
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal lacks messaging write authority."),
        "404": error_response("The message is absent or outside the exact participant principal."),
        "409": error_response("The message is deleted."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_messaging_message_reaction",
)


MESSAGE_REACTION_DELETE_CONTRACT = OperationContract(
    method="DELETE",
    summary="Remove the current principal's message reaction",
    description=(
        "Soft-removes only this exact principal's matching reaction. Missing/already removed "
        "reactions are idempotent and do not append duplicate realtime events."
    ),
    security=UNSAFE_SESSION_SECURITY,
    permission="messaging:write",
    parameters=(_MESSAGE_ID_PARAMETER, _EMOJI_PARAMETER),
    responses={
        "204": {"description": "Reaction is absent or soft-removed."},
        "400": error_response("The path value is not a valid emoji reaction."),
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal lacks messaging write authority."),
        "404": error_response("The message is absent or outside the exact participant principal."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="delete_messaging_message_reaction",
)
