"""Fail-closed executable OpenAPI contracts for the admissions CRM."""

from __future__ import annotations

from typing import Any

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
)


def _object(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] | None = None,
    nullable: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required if required is not None else properties),
    }
    if nullable:
        schema["nullable"] = True
    return schema


_ID = {"type": "integer", "minimum": 1}
_NULL_ID = {**_ID, "nullable": True}
_DATETIME = {"type": "string", "format": "date-time"}
_NULL_DATETIME = {**_DATETIME, "nullable": True}
_DATE = {"type": "string", "format": "date"}
_NULL_STRING = {"type": "string", "nullable": True}
_OWNER_INPUT = _object(
    {
        "kind": {"type": "string", "enum": ["staff", "teacher"]},
        "id": _ID,
    }
)
_PRINCIPAL_OUTPUT = _object(
    {
        "kind": {"type": "string", "enum": ["staff", "teacher"]},
        "id": _ID,
        "display_name": _NULL_STRING,
        "attribution_status": {"type": "string", "enum": ["captured", "unavailable"]},
    }
)
_OWNER_OUTPUT = {**_PRINCIPAL_OUTPUT, "nullable": True}
_STAGE = _object(
    {
        "id": _ID,
        "slug": {"type": "string", "maxLength": 64},
        "name": {"type": "string", "maxLength": 120},
        "category": {"type": "string", "enum": ["open", "won", "lost"]},
        "position": {"type": "integer", "minimum": 1, "maximum": 32767},
        "is_active": {"type": "boolean"},
        "created_at": _DATETIME,
        "updated_at": _DATETIME,
    }
)
_SOURCE = _object(
    {
        "id": _ID,
        "slug": {"type": "string", "maxLength": 64},
        "name": {"type": "string", "maxLength": 120},
        "is_active": {"type": "boolean"},
        "created_at": _DATETIME,
        "updated_at": _DATETIME,
    }
)
_CAMPAIGN_REF = _object({"id": _ID, "code": {"type": "string"}, "name": {"type": "string"}}, nullable=True)
_CAMPAIGN = _object(
    {
        "id": _ID,
        "code": {"type": "string", "maxLength": 64},
        "name": {"type": "string", "maxLength": 160},
        "source": _SOURCE,
        "branch": _NULL_ID,
        "branch_name": _NULL_STRING,
        "department": _NULL_ID,
        "department_name": _NULL_STRING,
        "starts_on": {**_DATE, "nullable": True},
        "ends_on": {**_DATE, "nullable": True},
        "is_active": {"type": "boolean"},
        "created_at": _DATETIME,
        "updated_at": _DATETIME,
    }
)
_STUDENT_REF = _object(
    {
        "id": _ID,
        "public_id": {"type": "string"},
        "full_name": {"type": "string"},
        "phone": _NULL_STRING,
        "email": _NULL_STRING,
        "status": {"type": "string"},
        "is_active": {"type": "boolean"},
    }
)
_LEAD = _object(
    {
        "id": _ID,
        "student": _STUDENT_REF,
        "branch": _ID,
        "branch_name": {"type": "string"},
        "department": _NULL_ID,
        "department_name": _NULL_STRING,
        "stage": _STAGE,
        "state": {"type": "string", "enum": ["open", "won", "lost", "merged"]},
        "owner": _OWNER_OUTPUT,
        "initial_source": _SOURCE,
        "initial_campaign": _CAMPAIGN_REF,
        "next_follow_up_at": _NULL_DATETIME,
        "loss_reason": _NULL_STRING,
        "canonical_lead": _NULL_ID,
        "version": {"type": "integer", "minimum": 1},
        "created_at": _DATETIME,
        "updated_at": _DATETIME,
    }
)
_STAGE_REF = _object({"id": _ID, "slug": {"type": "string"}, "name": {"type": "string"}}, nullable=True)
_HISTORY = _object(
    {
        "id": _ID,
        "lead": _ID,
        "from_stage": _STAGE_REF,
        "to_stage": {**_STAGE_REF, "nullable": False},
        "from_state": {"type": "string"},
        "to_state": {"type": "string"},
        "loss_reason": _NULL_STRING,
        "note": _NULL_STRING,
        "actor": _OWNER_INPUT,
        "created_at": _DATETIME,
    }
)
_TOUCH = _object(
    {
        "id": _ID,
        "lead": _ID,
        "channel": {
            "type": "string",
            "enum": ["phone", "sms", "email", "whatsapp", "in_person", "other"],
        },
        "direction": {"type": "string", "enum": ["inbound", "outbound"]},
        "outcome": _NULL_STRING,
        "summary": {"type": "string", "maxLength": 2000},
        "occurred_at": _DATETIME,
        "actor": _OWNER_INPUT,
        "created_at": _DATETIME,
    }
)
_FOLLOW_UP_LEAD = _object(
    {
        "id": _ID,
        "student": _object(
            {
                "id": _ID,
                "public_id": {"type": "string"},
                "full_name": {"type": "string"},
            }
        ),
        "branch": _ID,
        "branch_name": {"type": "string"},
        "department": _NULL_ID,
        "department_name": _NULL_STRING,
    }
)
_FOLLOW_UP = _object(
    {
        "id": _ID,
        "lead": _ID,
        "lead_summary": _FOLLOW_UP_LEAD,
        "due_at": _DATETIME,
        "purpose": {"type": "string", "maxLength": 500},
        "status": {"type": "string", "enum": ["pending", "completed", "cancelled"]},
        "assignee": _PRINCIPAL_OUTPUT,
        "created_by": _PRINCIPAL_OUTPUT,
        "resolved_by": _OWNER_OUTPUT,
        "resolution_note": _NULL_STRING,
        "resolved_at": _NULL_DATETIME,
        "created_at": _DATETIME,
        "updated_at": _DATETIME,
    }
)
_ATTRIBUTION = _object(
    {
        "id": _ID,
        "lead": _ID,
        "source": _SOURCE,
        "campaign": _CAMPAIGN_REF,
        "medium": _NULL_STRING,
        "content": _NULL_STRING,
        "occurred_at": _DATETIME,
        "actor": _OWNER_INPUT,
        "created_at": _DATETIME,
    }
)
_DUPLICATE_SIDE = _object(
    {
        "id": _ID,
        "student_public_id": {"type": "string"},
        "student_name": {"type": "string"},
    }
)
_DUPLICATE = _object(
    {
        "id": _ID,
        "left": _DUPLICATE_SIDE,
        "right": _DUPLICATE_SIDE,
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "signals": {
            "type": "array",
            "items": {"type": "string", "enum": ["phone", "email", "name_birthdate"]},
        },
        "status": {"type": "string", "enum": ["pending", "dismissed", "merged"]},
        "detected_at": _DATETIME,
        "reviewed_by": {**_OWNER_INPUT, "nullable": True},
        "reviewed_at": _NULL_DATETIME,
        "rationale": _NULL_STRING,
    }
)
_MERGE = _object(
    {
        "id": _ID,
        "candidate": _ID,
        "canonical_lead": _ID,
        "duplicate_lead": _ID,
        "rationale": {"type": "string"},
        "reviewed_by": _OWNER_INPUT,
        "created_at": _DATETIME,
    }
)
_PAGINATION = _object(
    {
        "total": {"type": "integer", "minimum": 0},
        "page": {"type": "integer", "minimum": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
        "pages": {"type": "integer", "minimum": 0},
        "has_next": {"type": "boolean"},
        "has_prev": {"type": "boolean"},
    }
)


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return _object(
        {"success": {"type": "boolean", "enum": [True]}, "data": data},
        required=("success", "data"),
    )


def _page(item: dict[str, Any]) -> dict[str, Any]:
    return _object(
        {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"type": "array", "items": item},
            "pagination": _PAGINATION,
        }
    )


def _json_response(description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": schema}},
    }


def _request(
    properties: dict[str, Any],
    required: tuple[str, ...],
    *,
    body_required: bool = True,
) -> dict[str, Any]:
    return {
        "required": body_required,
        "content": {
            "application/json": {
                "schema": _object(properties, required=required),
            }
        },
    }


def _errors(*, not_found: bool = False, conflict: bool = False) -> dict[str, Any]:
    responses = {
        "400": error_response("The request DTO or a declared selector is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The exact role principal lacks CRM authority in this scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    }
    if not_found:
        responses["404"] = error_response("The record does not exist inside the caller's CRM scope.")
    if conflict:
        responses["409"] = error_response("Lifecycle, version, duplicate-review, or idempotency conflict.")
    return responses


def _read(
    method: str,
    *,
    summary: str,
    schema: dict[str, Any],
    parameters: tuple[dict[str, Any], ...] = (),
    not_found: bool = False,
    operation_id: str,
) -> OperationContract:
    return OperationContract(
        method=method,
        summary=summary,
        description=(
            "Unknown and duplicate query parameters are rejected. Authorization uses the exact "
            "permission-bearing branch/department membership; out-of-scope identifiers return 404."
        ),
        permission="crm:read",
        security=SESSION_SECURITY,
        parameters=parameters,
        responses={
            "200": _json_response(
                "Scoped CRM response." if method == "GET" else "Scoped response metadata.", schema
            ),
            **_errors(not_found=not_found),
        },
        operation_id=operation_id,
    )


def _write(
    *,
    summary: str,
    body: dict[str, Any],
    response_schema: dict[str, Any],
    permission: str = "crm:write",
    created: bool = False,
    not_found: bool = False,
    conflict: bool = True,
    idempotent: bool = False,
    operation_id: str,
) -> OperationContract:
    parameters: tuple[dict[str, Any], ...] = ()
    if idempotent:
        parameters = (
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "minLength": 8, "maxLength": 128},
                "description": "Bound to the exact role principal and request fingerprint.",
            },
        )
    response = _json_response("Mutation result.", response_schema)
    if idempotent:
        response["headers"] = {
            "Idempotency-Replayed": {
                "description": "true only when the original saved result was replayed.",
                "schema": {"type": "string", "enum": ["true", "false"]},
            }
        }
    responses = {"201" if created else "200": response, **_errors(not_found=not_found, conflict=conflict)}
    if created and idempotent:
        responses["200"] = _json_response("Identical stored mutation result replayed.", response_schema)
        responses["200"]["headers"] = response["headers"]
    return OperationContract(
        method="POST",
        summary=summary,
        description=(
            "The JSON DTO is closed: unknown fields fail. Retry-sensitive mutations are "
            "serialized and replay the original result only for an identical principal/request."
        ),
        permission=permission,
        security=UNSAFE_SESSION_SECURITY,
        parameters=parameters,
        request_body=body,
        responses=responses,
        operation_id=operation_id,
    )


_PAGE = (
    {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
    {
        "name": "page_size",
        "in": "query",
        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)
_SEARCH_ORDER_PAGE = (
    {"name": "active", "in": "query", "schema": {"type": "boolean"}},
    {
        "name": "search",
        "in": "query",
        "schema": {"type": "string", "minLength": 2, "maxLength": 200},
    },
    {"name": "ordering", "in": "query", "schema": {"type": "string"}},
    *_PAGE,
)

STAGES_COLLECTION_CONTRACTS = (
    _read(
        "GET",
        summary="List CRM pipeline stages",
        schema=_page(_STAGE),
        parameters=_SEARCH_ORDER_PAGE,
        operation_id="get_crm_stages",
    ),
    _read(
        "HEAD",
        summary="Validate CRM pipeline-stage access",
        schema=_page(_STAGE),
        parameters=_SEARCH_ORDER_PAGE,
        operation_id="head_crm_stages",
    ),
    _write(
        summary="Create a tenant pipeline stage",
        permission="crm:manage",
        body=_request(
            {
                "slug": {"type": "string", "maxLength": 64},
                "name": {"type": "string", "maxLength": 120},
                "category": {"type": "string", "enum": ["open", "won", "lost"]},
                "position": {"type": "integer", "minimum": 1, "maximum": 32767},
            },
            ("slug", "name", "category", "position"),
        ),
        response_schema=_success(_STAGE),
        created=True,
        idempotent=True,
        operation_id="post_crm_stage",
    ),
)

STAGE_DETAIL_CONTRACTS = (
    _read(
        "GET",
        summary="Read a pipeline stage",
        schema=_success(_STAGE),
        not_found=True,
        operation_id="get_crm_stage",
    ),
    _read(
        "HEAD",
        summary="Validate pipeline-stage access",
        schema=_success(_STAGE),
        not_found=True,
        operation_id="head_crm_stage",
    ),
    OperationContract(
        method="PATCH",
        summary="Update or retire a pipeline stage",
        permission="crm:manage",
        security=UNSAFE_SESSION_SECURITY,
        request_body=_request(
            {
                "slug": {"type": "string", "maxLength": 64},
                "name": {"type": "string", "maxLength": 120},
                "category": {"type": "string", "enum": ["open", "won", "lost"]},
                "position": {"type": "integer", "minimum": 1, "maximum": 32767},
                "is_active": {"type": "boolean"},
            },
            (),
        ),
        responses={
            "200": _json_response("Updated stage.", _success(_STAGE)),
            **_errors(not_found=True, conflict=True),
        },
        operation_id="patch_crm_stage",
    ),
)

SOURCES_COLLECTION_CONTRACTS = (
    _read(
        "GET",
        summary="List CRM lead sources",
        schema=_page(_SOURCE),
        parameters=_SEARCH_ORDER_PAGE,
        operation_id="get_crm_sources",
    ),
    _read(
        "HEAD",
        summary="Validate lead-source access",
        schema=_page(_SOURCE),
        parameters=_SEARCH_ORDER_PAGE,
        operation_id="head_crm_sources",
    ),
    _write(
        summary="Create a lead source",
        permission="crm:manage",
        body=_request(
            {"slug": {"type": "string", "maxLength": 64}, "name": {"type": "string", "maxLength": 120}},
            ("slug", "name"),
        ),
        response_schema=_success(_SOURCE),
        created=True,
        idempotent=True,
        operation_id="post_crm_source",
    ),
)

_CAMPAIGN_FILTERS = (
    {"name": "active", "in": "query", "schema": {"type": "boolean"}},
    *({"name": name, "in": "query", "schema": _ID} for name in ("branch", "department", "source")),
    {
        "name": "search",
        "in": "query",
        "schema": {"type": "string", "minLength": 2, "maxLength": 200},
    },
    {"name": "ordering", "in": "query", "schema": {"type": "string"}},
    *_PAGE,
)
CAMPAIGNS_COLLECTION_CONTRACTS = (
    _read(
        "GET",
        summary="List scoped acquisition campaigns",
        schema=_page(_CAMPAIGN),
        parameters=_CAMPAIGN_FILTERS,
        operation_id="get_crm_campaigns",
    ),
    _read(
        "HEAD",
        summary="Validate acquisition-campaign access",
        schema=_page(_CAMPAIGN),
        parameters=_CAMPAIGN_FILTERS,
        operation_id="head_crm_campaigns",
    ),
    _write(
        summary="Create a scoped acquisition campaign",
        body=_request(
            {
                "code": {"type": "string", "maxLength": 64},
                "name": {"type": "string", "maxLength": 160},
                "source": _ID,
                "branch": _NULL_ID,
                "department": _NULL_ID,
                "starts_on": {**_DATE, "nullable": True},
                "ends_on": {**_DATE, "nullable": True},
            },
            ("code", "name", "source"),
        ),
        response_schema=_success(_CAMPAIGN),
        created=True,
        idempotent=True,
        operation_id="post_crm_campaign",
    ),
)

_LEAD_FILTERS = (
    *(
        {"name": name, "in": "query", "schema": _ID}
        for name in ("branch", "department", "stage", "owner_id", "source", "campaign")
    ),
    {"name": "state", "in": "query", "schema": {"type": "string", "enum": ["open", "won", "lost", "merged"]}},
    {"name": "owner_kind", "in": "query", "schema": {"type": "string", "enum": ["staff", "teacher"]}},
    *(
        {"name": name, "in": "query", "schema": _DATE}
        for name in ("follow_up_from", "follow_up_to", "date_from", "date_to")
    ),
    {
        "name": "search",
        "in": "query",
        "schema": {"type": "string", "minLength": 2, "maxLength": 200},
    },
    {"name": "ordering", "in": "query", "schema": {"type": "string"}},
    *_PAGE,
)
LEADS_COLLECTION_CONTRACTS = (
    _read(
        "GET",
        summary="List scoped CRM leads",
        schema=_page(_LEAD),
        parameters=_LEAD_FILTERS,
        operation_id="get_crm_leads",
    ),
    _read(
        "HEAD",
        summary="Validate CRM lead-list access",
        schema=_page(_LEAD),
        parameters=_LEAD_FILTERS,
        operation_id="head_crm_leads",
    ),
    _write(
        summary="Attach a student lead to the CRM workflow",
        body=_request(
            {
                "student": _ID,
                "stage": _ID,
                "department": _NULL_ID,
                "owner": {**_OWNER_INPUT, "nullable": True},
                "source": _ID,
                "campaign": _NULL_ID,
                "medium": {"type": "string", "maxLength": 64},
                "content": {"type": "string", "maxLength": 160},
                "attribution_occurred_at": _NULL_DATETIME,
            },
            ("student", "stage", "source"),
        ),
        response_schema=_success(_LEAD),
        created=True,
        not_found=True,
        idempotent=True,
        operation_id="post_crm_lead",
    ),
)

LEAD_DETAIL_CONTRACTS = tuple(
    _read(
        method,
        summary="Read a scoped CRM lead" if method == "GET" else "Validate scoped CRM lead access",
        schema=_success(_LEAD),
        not_found=True,
        operation_id=f"{method.lower()}_crm_lead",
    )
    for method in ("GET", "HEAD")
)

OWNER_CONTRACT = _write(
    summary="Assign or clear an exact role-native lead owner",
    body=_request({"owner": {**_OWNER_INPUT, "nullable": True}}, ("owner",)),
    response_schema=_success(_LEAD),
    not_found=True,
    idempotent=True,
    operation_id="post_crm_lead_owner",
)
TRANSITION_CONTRACT = _write(
    summary="Transition a lead through the immutable pipeline history",
    body=_request(
        {
            "stage": _ID,
            "expected_version": {"type": "integer", "minimum": 1},
            "loss_reason": {"type": "string", "maxLength": 255},
            "note": {"type": "string", "maxLength": 1000},
        },
        ("stage", "expected_version"),
    ),
    response_schema=_success(_HISTORY),
    not_found=True,
    idempotent=True,
    operation_id="post_crm_lead_transition",
)


def _timeline_reads(name: str, item: dict[str, Any], *, extra=()) -> tuple[OperationContract, ...]:
    return tuple(
        _read(
            method,
            summary=f"{'List' if method == 'GET' else 'Validate'} lead {name}",
            schema=_page(item),
            parameters=(*extra, *_PAGE),
            not_found=True,
            operation_id=f"{method.lower()}_crm_lead_{name.replace('-', '_')}",
        )
        for method in ("GET", "HEAD")
    )


STAGE_HISTORY_CONTRACTS = _timeline_reads("stage-history", _HISTORY)
TOUCH_CONTRACTS = (
    *_timeline_reads(
        "touches",
        _TOUCH,
        extra=(
            {"name": "channel", "in": "query", "schema": {"type": "string"}},
            {"name": "direction", "in": "query", "schema": {"type": "string"}},
            {"name": "date_from", "in": "query", "schema": _DATE},
            {"name": "date_to", "in": "query", "schema": _DATE},
        ),
    ),
    _write(
        summary="Append a communication touch",
        body=_request(
            {
                "channel": {
                    "type": "string",
                    "enum": ["phone", "sms", "email", "whatsapp", "in_person", "other"],
                },
                "direction": {"type": "string", "enum": ["inbound", "outbound"]},
                "outcome": {"type": "string", "maxLength": 64},
                "summary": {"type": "string", "maxLength": 2000},
                "occurred_at": _NULL_DATETIME,
            },
            ("channel", "direction", "summary"),
        ),
        response_schema=_success(_TOUCH),
        created=True,
        not_found=True,
        idempotent=True,
        operation_id="post_crm_lead_touch",
    ),
)
FOLLOW_UP_CONTRACTS = (
    *_timeline_reads(
        "follow-ups",
        _FOLLOW_UP,
        extra=({"name": "status", "in": "query", "schema": {"type": "string"}},),
    ),
    _write(
        summary="Schedule a scoped lead follow-up",
        body=_request(
            {
                "due_at": _DATETIME,
                "purpose": {"type": "string", "maxLength": 500},
                "assignee": {**_OWNER_INPUT, "nullable": True},
            },
            ("due_at", "purpose"),
        ),
        response_schema=_success(_FOLLOW_UP),
        created=True,
        not_found=True,
        idempotent=True,
        operation_id="post_crm_lead_follow_up",
    ),
)
FOLLOW_UP_REGISTER_PARAMETERS = (
    *({"name": name, "in": "query", "schema": _ID} for name in ("branch", "department", "assignee_id")),
    {
        "name": "assignee_kind",
        "in": "query",
        "schema": {"type": "string", "enum": ["staff", "teacher"]},
    },
    {
        "name": "status",
        "in": "query",
        "schema": {"type": "string", "enum": ["pending", "completed", "cancelled"]},
    },
    {"name": "due_from", "in": "query", "schema": _DATE},
    {"name": "due_to", "in": "query", "schema": _DATE},
    {
        "name": "search",
        "in": "query",
        "schema": {"type": "string", "minLength": 2, "maxLength": 200},
    },
    {"name": "ordering", "in": "query", "schema": {"type": "string"}},
    *_PAGE,
)
FOLLOW_UP_REGISTER_CONTRACTS = tuple(
    _read(
        method,
        summary=(
            "List scoped CRM follow-up work"
            if method == "GET"
            else "Validate scoped CRM follow-up work access"
        ),
        schema=_page(_FOLLOW_UP),
        parameters=FOLLOW_UP_REGISTER_PARAMETERS,
        operation_id=f"{method.lower()}_crm_follow_up_register",
    )
    for method in ("GET", "HEAD")
)
ATTRIBUTION_CONTRACTS = (
    *_timeline_reads(
        "attributions",
        _ATTRIBUTION,
        extra=(
            {"name": "source", "in": "query", "schema": _ID},
            {"name": "campaign", "in": "query", "schema": _ID},
        ),
    ),
    _write(
        summary="Append source/campaign attribution",
        body=_request(
            {
                "source": _ID,
                "campaign": _NULL_ID,
                "medium": {"type": "string", "maxLength": 64},
                "content": {"type": "string", "maxLength": 160},
                "occurred_at": _NULL_DATETIME,
            },
            ("source",),
        ),
        response_schema=_success(_ATTRIBUTION),
        created=True,
        not_found=True,
        idempotent=True,
        operation_id="post_crm_lead_attribution",
    ),
)
DETECT_DUPLICATES_CONTRACT = _write(
    summary="Detect duplicate leads using non-reversible indexed signals",
    body=_request({}, (), body_required=False),
    response_schema=_success({"type": "array", "items": _DUPLICATE}),
    not_found=True,
    idempotent=True,
    operation_id="post_crm_detect_duplicates",
)

_DUPLICATE_FILTERS = (
    {
        "name": "status",
        "in": "query",
        "schema": {"type": "string", "enum": ["pending", "dismissed", "merged"]},
    },
    {"name": "ordering", "in": "query", "schema": {"type": "string"}},
    *_PAGE,
)
DUPLICATES_COLLECTION_CONTRACTS = tuple(
    _read(
        method,
        summary="List duplicate-review candidates" if method == "GET" else "Validate duplicate-review access",
        schema=_page(_DUPLICATE),
        parameters=_DUPLICATE_FILTERS,
        operation_id=f"{method.lower()}_crm_duplicates",
    )
    for method in ("GET", "HEAD")
)
DUPLICATE_DISMISS_CONTRACT = _write(
    summary="Dismiss a reviewed duplicate candidate",
    body=_request({"rationale": {"type": "string", "maxLength": 1000}}, ("rationale",)),
    response_schema=_success(_DUPLICATE),
    not_found=True,
    idempotent=True,
    operation_id="post_crm_duplicate_dismiss",
)
DUPLICATE_MERGE_CONTRACT = _write(
    summary="Canonicalize a reviewed duplicate without deleting identity or history",
    body=_request(
        {"rationale": {"type": "string", "maxLength": 1000}, "canonical_lead": _ID},
        ("rationale", "canonical_lead"),
    ),
    response_schema=_success(_MERGE),
    not_found=True,
    idempotent=True,
    operation_id="post_crm_duplicate_merge",
)


def follow_up_action_contract(action: str) -> OperationContract:
    return _write(
        summary=f"{action.title()} a pending lead follow-up",
        body=_request(
            {"note": {"type": "string", "maxLength": 1000}},
            (),
            body_required=False,
        ),
        response_schema=_success(_FOLLOW_UP),
        not_found=True,
        idempotent=True,
        operation_id=f"post_crm_follow_up_{action}",
    )


FUNNEL_PARAMETERS = tuple(
    [
        {"name": "date_from", "in": "query", "required": True, "schema": _DATE},
        {"name": "date_to", "in": "query", "required": True, "schema": _DATE},
    ]
    + [
        {"name": name, "in": "query", "schema": _ID}
        for name in ("branch", "department", "source", "campaign")
    ]
)
_FUNNEL = _object(
    {
        "generated_at": _DATETIME,
        "window": _object(
            {
                "date_from": _DATE,
                "date_to": _DATE,
                "timezone": {"type": "string"},
                "basis": {"type": "string", "enum": ["lead_created_at"]},
                "inclusive": {"type": "boolean", "enum": [True]},
            }
        ),
        "scope": _object(
            {
                "authorization": _object(
                    {
                        "organization_wide": {"type": "boolean"},
                        "branch_wide": {"type": "array", "items": _ID},
                        "departments": {
                            "type": "array",
                            "items": _object({"branch": _ID, "department": _ID}),
                        },
                    }
                ),
                "filters": _object(
                    {
                        "branch": _NULL_ID,
                        "department": _NULL_ID,
                        "source": _NULL_ID,
                        "campaign": _NULL_ID,
                    }
                ),
            }
        ),
        "sample_size": {"type": "integer", "minimum": 0},
        "excluded_merged_count": {"type": "integer", "minimum": 0},
        "states": _object(
            {
                "open": {"type": "integer", "minimum": 0},
                "won": {"type": "integer", "minimum": 0},
                "lost": {"type": "integer", "minimum": 0},
            }
        ),
        "conversion_fraction": {"type": "number", "minimum": 0, "maximum": 1, "nullable": True},
        "loss_fraction": {"type": "number", "minimum": 0, "maximum": 1, "nullable": True},
        "stages": {
            "type": "array",
            "items": _object(
                {
                    "id": _ID,
                    "slug": {"type": "string"},
                    "name": {"type": "string"},
                    "position": {"type": "integer", "minimum": 1},
                    "count": {"type": "integer", "minimum": 0},
                    "fraction": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "nullable": True,
                    },
                }
            ),
        },
        "definitions": _object(
            {
                "sample_size": {"type": "string"},
                "conversion_fraction": {"type": "string"},
                "loss_fraction": {"type": "string"},
            }
        ),
    }
)
FUNNEL_CONTRACTS = tuple(
    _read(
        method,
        summary="Compute an exact scoped CRM funnel" if method == "GET" else "Validate CRM funnel access",
        schema=_success(_FUNNEL),
        parameters=FUNNEL_PARAMETERS,
        operation_id=f"{method.lower()}_crm_funnel",
    )
    for method in ("GET", "HEAD")
)
