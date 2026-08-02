"""Executable OpenAPI contracts for the scoped, append-only audit timeline."""

from apps.audit.models import AuditLog
from core.openapi_contracts import SESSION_SECURITY, OperationContract, error_response, json_response

_FILTER_PARAMETERS = (
    {
        "name": "actor",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "format": "int64", "minimum": 1},
    },
    {
        "name": "actor_principal_kind",
        "in": "query",
        "required": False,
        "description": "Must be supplied together with actor_principal_id.",
        "schema": {
            "type": "string",
            "enum": ["user", "student", "teacher", "parent", "staff"],
        },
    },
    {
        "name": "actor_principal_id",
        "in": "query",
        "required": False,
        "description": "Must be supplied together with actor_principal_kind.",
        "schema": {"type": "integer", "format": "int64", "minimum": 1},
    },
    {
        "name": "action",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": list(AuditLog.Action.values),
        },
    },
    {
        "name": "resource_type",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "maxLength": 100},
    },
    {
        "name": "resource_id",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "maxLength": 64},
    },
    {
        "name": "ts_from",
        "in": "query",
        "required": False,
        "description": "Inclusive timestamp with UTC offset.",
        "schema": {"type": "string", "format": "date-time"},
    },
    {
        "name": "ts_to",
        "in": "query",
        "required": False,
        "description": "Inclusive timestamp with UTC offset; must not precede ts_from.",
        "schema": {"type": "string", "format": "date-time"},
    },
    {
        "name": "branch",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "format": "int64", "minimum": 1},
    },
    {
        "name": "department",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "format": "int64", "minimum": 1},
    },
    {
        "name": "scope_status",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["scoped", "organization", "unresolved"]},
    },
    {
        "name": "sensitivity",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["standard", "compensation"]},
    },
)

_CURSOR_PARAMETERS = (
    *_FILTER_PARAMETERS,
    {
        "name": "cursor",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "maxLength": 512},
    },
    {
        "name": "page_size",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
    },
)

AUDIT_LIST_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Read the permission-scoped immutable audit timeline",
    description=(
        "Returns only rows intersecting the caller's exact audit:read scope. Compensation "
        "events additionally require compensation:read. Unknown or duplicate filters fail closed."
    ),
    permission="audit:read",
    security=SESSION_SECURITY,
    parameters=_CURSOR_PARAMETERS,
    responses={
        "200": json_response("Stable cursor page of audit evidence.", "AuditCursorPage"),
        "400": error_response("A filter, range, principal pair, page size, or cursor is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks audit read authority."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_audit_timeline",
)

AUDIT_LIST_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check the scoped audit timeline",
    description="Same authorization and filter semantics as GET, with no response body.",
    permission="audit:read",
    security=SESSION_SECURITY,
    parameters=_CURSOR_PARAMETERS,
    responses={
        "200": {"description": "The scoped audit timeline is available."},
        "400": error_response("A filter, range, principal pair, page size, or cursor is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks audit read authority."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_audit_timeline",
)

AUDIT_DETAIL_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Read one visible immutable audit event",
    permission="audit:read",
    security=SESSION_SECURITY,
    responses={
        "200": json_response("Visible audit evidence.", "AuditDetailResponse"),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks audit read authority."),
        "404": error_response("The event does not exist inside the caller's visible scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_audit_event",
)

AUDIT_DETAIL_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check one visible immutable audit event",
    permission="audit:read",
    security=SESSION_SECURITY,
    responses={
        "200": {"description": "The scoped event is available."},
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks audit read authority."),
        "404": error_response("The event does not exist inside the caller's visible scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_audit_event",
)

_CSV_RESPONSE = {
    "description": "CSV stream frozen to the exact authorized filter result.",
    "content": {"text/csv": {"schema": {"type": "string"}}},
    "headers": {
        "Content-Disposition": {
            "schema": {"type": "string"},
            "description": "Attachment filename for the audit export.",
        }
    },
}

AUDIT_EXPORT_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Export the permission-scoped audit timeline as CSV",
    description=(
        "Uses the same scope and filters as the screen, caps exports at 50,000 rows, "
        "neutralizes spreadsheet formulas, freezes concurrent inserts, and audits the export."
    ),
    permission="audit:read",
    security=SESSION_SECURITY,
    parameters=_FILTER_PARAMETERS,
    responses={
        "200": _CSV_RESPONSE,
        "400": error_response("Filters are invalid or more than 50,000 rows match."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks audit read authority."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="export_audit_timeline",
)

AUDIT_EXPORT_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check an audit export without creating evidence",
    description="Validates the same scope and filters without streaming rows or writing an export event.",
    permission="audit:read",
    security=SESSION_SECURITY,
    parameters=_FILTER_PARAMETERS,
    responses={
        "200": {"description": "The scoped export is available."},
        "400": error_response("Filters are invalid or more than 50,000 rows match."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks audit read authority."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_audit_export",
)


OPENAPI_SCHEMAS = {
    "AuditActorPrincipal": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["exact", "system", "unresolved"]},
            "kind": {
                "type": "string",
                "nullable": True,
                "enum": ["user", "student", "teacher", "parent", "staff"],
            },
            "id": {"type": "integer", "format": "int64", "minimum": 1, "nullable": True},
        },
        "required": ["status", "kind", "id"],
    },
    "AuditScope": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["scoped", "organization", "unresolved"]},
            "branch": {"type": "integer", "format": "int64", "minimum": 1, "nullable": True},
            "department": {
                "type": "integer",
                "format": "int64",
                "minimum": 1,
                "nullable": True,
            },
        },
        "required": ["status", "branch", "department"],
    },
    "AuditEvent": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "format": "int64", "minimum": 1},
            "actor": {"type": "integer", "format": "int64", "minimum": 1, "nullable": True},
            "actor_username": {"type": "string", "nullable": True},
            "actor_repr": {"type": "string"},
            "actor_principal": {"$ref": "#/components/schemas/AuditActorPrincipal"},
            "action": {"type": "string"},
            "resource_type": {"type": "string"},
            "resource_id": {"type": "string"},
            "before": {"type": "object", "nullable": True, "additionalProperties": True},
            "after": {"type": "object", "nullable": True, "additionalProperties": True},
            "ip": {"type": "string", "nullable": True},
            "user_agent": {"type": "string"},
            "scope": {"$ref": "#/components/schemas/AuditScope"},
            "sensitivity": {"type": "string", "enum": ["standard", "compensation"]},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "required": [
            "id",
            "actor",
            "actor_username",
            "actor_repr",
            "actor_principal",
            "action",
            "resource_type",
            "resource_id",
            "before",
            "after",
            "ip",
            "user_agent",
            "scope",
            "sensitivity",
            "created_at",
        ],
    },
    "AuditCursorPage": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "results": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/AuditEvent"},
            },
            "next": {"type": "string", "format": "uri", "nullable": True},
            "previous": {"type": "string", "format": "uri", "nullable": True},
        },
        "required": ["results", "next", "previous"],
    },
    "AuditDetailResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"$ref": "#/components/schemas/AuditEvent"},
        },
        "required": ["success", "data"],
    },
}
