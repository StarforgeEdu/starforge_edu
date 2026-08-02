"""Explicit contract for the print-job capability boundary."""

from __future__ import annotations

from typing import Any

from core.openapi_contracts import (
    BRANCH_AGENT_SECURITY,
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)


def _closed_success_schema(data_schema: dict) -> dict:
    """One inline success envelope that remains compatible with runtime warnings."""

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": data_schema,
            "warnings": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/RuntimeWarning"},
            },
        },
        "required": ["success", "data"],
    }


def _inline_json_response(description: str, schema: dict) -> dict:
    return {
        "description": description,
        "content": {"application/json": {"schema": schema}},
    }


_AGENT_PRINT_JOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "Device-minimized instructions and acknowledgement state. Tenant/branch, requester, "
        "source record id, cohort, storage key, device error text, and agent identity are omitted."
    ),
    "properties": {
        "id": {"type": "integer", "format": "int64", "minimum": 1},
        "printer": {"type": "integer", "format": "int64", "minimum": 1, "nullable": True},
        "status": {
            "type": "string",
            "enum": ["queued", "picked", "printing", "done", "failed"],
        },
        "source": {
            "type": "string",
            "enum": ["assignment", "transcript", "report", "receipt"],
        },
        "pages": {"type": "integer", "minimum": 1},
        "copies": {"type": "integer", "minimum": 1},
        "color": {"type": "boolean"},
        "duplex": {"type": "boolean"},
        "attempts": {"type": "integer", "minimum": 0, "maximum": 3},
        "next_attempt_at": {"type": "string", "format": "date-time", "nullable": True},
        "pages_printed": {"type": "integer", "minimum": 0},
        "lease_id": {"type": "string", "format": "uuid", "nullable": True},
        "lease_expires_at": {
            "type": "string",
            "format": "date-time",
            "nullable": True,
        },
    },
    "required": [
        "id",
        "printer",
        "status",
        "source",
        "pages",
        "copies",
        "color",
        "duplex",
        "attempts",
        "next_attempt_at",
        "pages_printed",
        "lease_id",
        "lease_expires_at",
    ],
}


def _agent_job_variant(*, statuses: list[str], active_lease: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {**_AGENT_PRINT_JOB_SCHEMA["properties"]}
    properties["status"] = {"type": "string", "enum": statuses}
    properties["lease_id"] = {
        "type": "string",
        "format": "uuid",
        **({} if active_lease else {"nullable": True, "enum": [None]}),
    }
    properties["lease_expires_at"] = {
        "type": "string",
        "format": "date-time",
        **({} if active_lease else {"nullable": True, "enum": [None]}),
    }
    return {**_AGENT_PRINT_JOB_SCHEMA, "properties": properties}


_ACTIVE_AGENT_JOB_SCHEMA = _agent_job_variant(
    statuses=["picked", "printing"],
    active_lease=True,
)
_CLOSED_AGENT_JOB_SCHEMA = _agent_job_variant(
    statuses=["queued", "done", "failed"],
    active_lease=False,
)

_AGENT_CLAIM_RESPONSE_SCHEMA = _closed_success_schema(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job": _agent_job_variant(statuses=["picked"], active_lease=True),
            "download_url": {
                "type": "string",
                "format": "uri",
                "description": "Short-lived signed URL for this claim's server-validated object key.",
            },
        },
        "required": ["job", "download_url"],
    }
)

_AGENT_STATUS_RESPONSE_SCHEMA = _closed_success_schema(
    {"oneOf": [_ACTIVE_AGENT_JOB_SCHEMA, _CLOSED_AGENT_JOB_SCHEMA]}
)
_AGENT_HEARTBEAT_RESPONSE_SCHEMA = _closed_success_schema(_ACTIVE_AGENT_JOB_SCHEMA)

_EMPTY_CLAIM_REQUEST = {
    "required": False,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "maxProperties": 0,
                "description": "The body may be omitted or be an empty JSON object.",
            }
        }
    },
}


def _status_request_variant(status: str, *, allow_error: bool) -> dict:
    properties = {
        "lease_id": {"type": "string", "format": "uuid"},
        "status": {"type": "string", "enum": [status]},
        "pages_printed": {"type": "integer", "minimum": 0},
    }
    if allow_error:
        properties["error"] = {
            "type": "string",
            "maxLength": 2000,
            "description": "Private device diagnostic; never returned to the device or staff API.",
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": ["lease_id", "status"],
    }


_AGENT_STATUS_REQUEST = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "oneOf": [
                    _status_request_variant("printing", allow_error=False),
                    _status_request_variant("done", allow_error=False),
                    _status_request_variant("failed", allow_error=True),
                ]
            }
        }
    },
}

_AGENT_HEARTBEAT_REQUEST = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "lease_id": {"type": "string", "format": "uuid"},
                    "pages_printed": {"type": "integer", "minimum": 0},
                },
                "required": ["lease_id"],
            }
        }
    },
}


AGENT_CLAIM_CONTRACTS = (
    OperationContract(
        method="POST",
        summary="Atomically claim the next branch print job",
        description=(
            "Authenticates only Authorization: Agent <64-hex-token>. The oldest eligible job "
            "in the agent's immutable branch is locked, its current domain source and object key "
            "are revalidated, and a short-lived URL is signed in the same transaction. A signing "
            "failure rolls the claim back; an invalid source is quarantined without exposing a URL."
        ),
        security=BRANCH_AGENT_SECURITY,
        request_body=_EMPTY_CLAIM_REQUEST,
        responses={
            "200": _inline_json_response(
                "A job was claimed and its validated document capability was issued.",
                _AGENT_CLAIM_RESPONSE_SCHEMA,
            ),
            "204": {"description": "No eligible job is currently queued for this branch."},
            "400": error_response("The optional body is not an empty JSON object."),
            "401": error_response("The Agent header is missing, malformed, unknown, or revoked."),
            "409": error_response("The claimed row's domain source is invalid and was quarantined."),
            "429": error_response("The request rate limit was exceeded."),
        },
        operation_id="post_printing_agent_claim",
    ),
)

AGENT_JOB_STATUS_CONTRACTS = (
    OperationContract(
        method="POST",
        summary="Report progress for the exact job claimed by this agent",
        description=(
            "The live token, branch, assigned agent id, per-attempt lease UUID, lease expiry, row "
            "lock, transition matrix, and monotonic page bound are rechecked atomically. An expired "
            "attempt or failure after printing began is quarantined for manual reconciliation and "
            "is never requeued automatically. Only a zero-progress failure before printing began "
            "uses the bounded automatic retry policy. The agent must receive a successful PRINTING "
            "transition before submitting bytes to a physical printer or spooler. Retrying that "
            "exact PRINTING transition is idempotent. DONE records the full authorized page total."
        ),
        security=BRANCH_AGENT_SECURITY,
        request_body=_AGENT_STATUS_REQUEST,
        parameters=(
            {
                "name": "job_id",
                "in": "path",
                "required": True,
                "schema": {"type": "integer", "format": "int64", "minimum": 1},
            },
        ),
        responses={
            "200": _inline_json_response(
                "The transition was applied; failed attempts may return to queued.",
                _AGENT_STATUS_RESPONSE_SCHEMA,
            ),
            "400": error_response("The closed DTO, error/status relationship, or page progress is invalid."),
            "401": error_response("The Agent header is missing, malformed, unknown, or revoked."),
            "404": error_response("The job is absent or is not assigned to this exact live agent."),
            "409": error_response(
                "The transition is illegal or the physical attempt requires operator reconciliation."
            ),
            "429": error_response("The request rate limit was exceeded."),
        },
        operation_id="post_printing_agent_job_status",
    ),
)

AGENT_JOB_HEARTBEAT_CONTRACTS = (
    OperationContract(
        method="POST",
        summary="Renew the exact active physical-print lease",
        description=(
            "Authenticates the branch device and matches its current per-attempt lease UUID under "
            "a row lock. A live PICKED or PRINTING lease is extended from server time and optional "
            "page progress remains monotonic and bounded. Expired work moves to "
            "reconciliation_required; this operation never requeues or issues another download URL."
        ),
        security=BRANCH_AGENT_SECURITY,
        request_body=_AGENT_HEARTBEAT_REQUEST,
        parameters=(
            {
                "name": "job_id",
                "in": "path",
                "required": True,
                "schema": {"type": "integer", "format": "int64", "minimum": 1},
            },
        ),
        responses={
            "200": _inline_json_response(
                "The active lease was renewed from authoritative server time.",
                _AGENT_HEARTBEAT_RESPONSE_SCHEMA,
            ),
            "400": error_response("The closed heartbeat DTO or page progress is invalid."),
            "401": error_response("The Agent header is missing, malformed, unknown, or revoked."),
            "404": error_response("The job or exact lease is not owned by this live agent."),
            "409": error_response("The lease expired and operator reconciliation is required."),
            "429": error_response("The request rate limit was exceeded."),
        },
        operation_id="post_printing_agent_job_heartbeat",
    ),
)

_LIST_PARAMETERS = (
    {
        "name": "status",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": [
                "queued",
                "picked",
                "printing",
                "reconciliation_required",
                "done",
                "failed",
            ],
        },
    },
    {
        "name": "source",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": ["assignment", "transcript", "report", "receipt"],
        },
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
        "schema": {"type": "string", "enum": ["created_at", "-created_at"]},
    },
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
        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)

JOBS_GET_CONTRACT = OperationContract(
    method="GET",
    summary="List visible print jobs",
    description=(
        "Returns only jobs inside the exact active memberships supplying printing:read. "
        "Storage keys, internal device errors, and agent credentials are never serialized."
    ),
    permission="printing:read",
    security=SESSION_SECURITY,
    parameters=_LIST_PARAMETERS,
    responses={
        "200": json_response("Scoped print-job page.", "PrintJobPageResponse"),
        "400": error_response("A list filter, ordering, or pagination value is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal lacks printing read authority."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_printing_jobs",
)

JOBS_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check visible print jobs",
    description="Same authorization and filter semantics as GET, without a response body.",
    permission="printing:read",
    security=SESSION_SECURITY,
    parameters=_LIST_PARAMETERS,
    responses={
        "200": json_response("Scoped print-job lookup completed."),
        "400": error_response("A list filter, ordering, or pagination value is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal lacks printing read authority."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_printing_jobs",
)

JOBS_POST_CONTRACT = OperationContract(
    method="POST",
    summary="Queue an authorized domain document for printing",
    description=(
        "The caller identifies an in-scope assignment attachment, completed transcript, "
        "single-branch report run, or confirmed payment receipt. The server derives the exact "
        "object key, branch, and cohort. Client-supplied storage or routing fields are rejected."
    ),
    permission="printing:write plus the selected source's read permission",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("PrintJobCreateRequest"),
    responses={
        "201": json_response("Print job queued or an identical open job returned.", "PrintJobResponse"),
        "400": error_response(
            "The DTO is malformed, has unsupported fields, or needs an assignment attachment selector."
        ),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response(
            "Permission, exact membership scope, read-only session, or cookie CSRF check failed."
        ),
        "404": error_response("The source is absent or outside the caller's visible scope."),
        "409": error_response(
            "The same open source/key exists with different pages, copies, color, duplex, or cohort."
        ),
        "422": error_response("The source file is not ready or the cohort print quota is exceeded."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_printing_jobs",
)

JOBS_COLLECTION_CONTRACTS = (
    JOBS_GET_CONTRACT,
    JOBS_HEAD_CONTRACT,
    JOBS_POST_CONTRACT,
)

_JOB_PK_PARAMETER = {
    "name": "pk",
    "in": "path",
    "required": True,
    "schema": {"type": "integer", "format": "int64", "minimum": 1},
}
_RECONCILIATION_PAGE_PARAMETERS = (
    _JOB_PK_PARAMETER,
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
        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)

JOB_RECONCILIATIONS_GET_CONTRACT = OperationContract(
    method="GET",
    summary="List reviewed physical-output evidence for one visible print job",
    description=(
        "Requires printing:read and resolves the parent job through the caller's exact active "
        "branch memberships before reading append-only reconciliation evidence. Raw lease and "
        "idempotency capabilities are omitted."
    ),
    permission="printing:read",
    security=SESSION_SECURITY,
    parameters=_RECONCILIATION_PAGE_PARAMETERS,
    responses={
        "200": json_response(
            "Scoped reconciliation evidence page.",
            "PrintJobReconciliationPageResponse",
        ),
        "400": error_response("Pagination is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal lacks printing read authority."),
        "404": error_response("The job is absent or outside the caller's visible scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_printing_job_reconciliations",
)

JOB_RECONCILIATIONS_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check visible print reconciliation evidence",
    description="Same authorization and scope semantics as GET, without a response body.",
    permission="printing:read",
    security=SESSION_SECURITY,
    parameters=_RECONCILIATION_PAGE_PARAMETERS,
    responses={
        "200": {"description": "The scoped reconciliation page exists."},
        "400": error_response("Pagination is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal lacks printing read authority."),
        "404": error_response("The job is absent or outside the caller's visible scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_printing_job_reconciliations",
)

JOB_RECONCILIATIONS_CONTRACTS = (
    JOB_RECONCILIATIONS_GET_CONTRACT,
    JOB_RECONCILIATIONS_HEAD_CONTRACT,
)

JOB_RECONCILE_CONTRACTS = (
    OperationContract(
        method="POST",
        summary="Resolve one quarantined physical-print attempt from reviewed evidence",
        description=(
            "Requires printing:write in the job's exact branch plus a mandatory Idempotency-Key. "
            "confirmed_printed closes DONE; confirmed_not_printed alone may schedule a bounded "
            "retry; abandoned_unknown closes FAILED. It never blindly duplicates paper."
        ),
        permission="printing:write",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request("PrintJobReconcileRequest"),
        parameters=(
            _JOB_PK_PARAMETER,
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {
                    "type": "string",
                    "minLength": 16,
                    "maxLength": 128,
                    "pattern": r"^[\x21-\x7e]{16,128}$",
                },
            },
        ),
        responses={
            "200": json_response("The reviewed outcome was applied idempotently.", "PrintJobResponse"),
            "400": error_response("The closed DTO, evidence reference, or idempotency key is invalid."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response(
                "Permission, branch scope, read-only session, or cookie CSRF check failed."
            ),
            "404": error_response("The job is absent or outside the caller's write scope."),
            "409": error_response(
                "The job is not quarantined, evidence state is incomplete, or the key was reused."
            ),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id="post_printing_job_reconcile",
    ),
)
