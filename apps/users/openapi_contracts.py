"""Explicit OpenAPI metadata for the current-principal bootstrap operation."""

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)

_SESSION_PAGE_PARAMETERS = (
    {
        "name": "page",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "default": 1},
    },
    {
        "name": "page_size",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
    },
)

ME_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Bootstrap the current identity and effective authorization scope",
    description=(
        "Canonical management bootstrap after role login. Effective permissions and scopes "
        "are calculated for only the current live principal; inactive account types and revoked "
        "memberships are excluded. Organization locale, IANA timezone, currency, mandatory "
        "password state, and read-only session state are authoritative client defaults."
    ),
    security=SESSION_SECURITY,
    responses={
        "200": json_response(
            "Current identity and authorization bootstrap.",
            "UserBootstrapResponse",
        ),
        "401": error_response(
            "Session is absent, invalid, expired, revoked, or no longer maps to an active principal."
        ),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_users_me_bootstrap",
)

ME_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check the current identity bootstrap",
    description="Same authorization and status semantics as GET, with no response body.",
    security=SESSION_SECURITY,
    responses={
        "200": json_response("The current session can bootstrap its identity."),
        "401": error_response(
            "Session is absent, invalid, expired, revoked, or no longer maps to an active principal."
        ),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_users_me_bootstrap",
)

ME_PATCH_CONTRACT = OperationContract(
    method="PATCH",
    summary="Update supported fields on the current profile",
    description=(
        "Self-scoped identity update. Administrative activation fields are rejected and a "
        "read-only session cannot mutate the profile."
    ),
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("UserProfileUpdateRequest", required=False),
    responses={
        "200": json_response(
            "Updated identity and refreshed authorization bootstrap.",
            "UserBootstrapResponse",
        ),
        "400": error_response("A supplied profile field is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("Cookie CSRF validation failed or the session is read-only."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="patch_users_me_profile",
)

SESSIONS_GET_CONTRACT = OperationContract(
    method="GET",
    summary="List the current principal's authenticated sessions",
    description=(
        "Returns only live sessions belonging to the exact role-native principal used by "
        "this request. Rows expose coarse device/browser labels and timestamps, never a raw "
        "credential, credential digest, IP address, device identifier, or user-agent string."
    ),
    security=SESSION_SECURITY,
    parameters=_SESSION_PAGE_PARAMETERS,
    responses={
        "200": json_response("Current principal session register.", "SessionPageResponse"),
        "400": error_response("Pagination or query parameters are invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_users_sessions",
)

SESSIONS_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check the current principal's session register",
    description="Same authorization and status semantics as GET, with no response body.",
    security=SESSION_SECURITY,
    parameters=_SESSION_PAGE_PARAMETERS,
    responses={
        "200": json_response("Session register is available."),
        "400": error_response("Pagination or query parameters are invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_users_sessions",
)

SESSION_DELETE_CONTRACT = OperationContract(
    method="DELETE",
    summary="Revoke one current-principal session",
    description=(
        "Revokes one live session visible to the exact authenticated principal. An identifier "
        "belonging to another role account returns not found. Revoking the current cookie session "
        "also expires its browser cookie."
    ),
    security=UNSAFE_SESSION_SECURITY,
    responses={
        "204": {"description": "Session revoked."},
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("Cookie CSRF validation failed or the session is read-only."),
        "404": error_response("Session is absent or outside the current principal."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="delete_users_session",
)


OPENAPI_SCHEMAS = {
    "AuthenticatedSession": {
        "type": "object",
        "additionalProperties": False,
        "description": (
            "Privacy-minimized live session for the exact current role-native principal. "
            "Raw credentials, digests, IP addresses, device identifiers, and user-agent "
            "strings are never exposed."
        ),
        "properties": {
            "id": {"type": "integer", "format": "int64", "minimum": 1},
            "platform": {"type": "string", "enum": ["web", "ios", "android"]},
            "device": {
                "type": "string",
                "enum": [
                    "Windows",
                    "macOS",
                    "Linux",
                    "ChromeOS",
                    "Android",
                    "iPhone",
                    "iPad",
                    "Unknown",
                ],
            },
            "browser": {
                "type": "string",
                "enum": ["Edge", "Firefox", "Chrome", "Safari", "Other"],
            },
            "created_at": {"type": "string", "format": "date-time"},
            "last_activity_at": {"type": "string", "format": "date-time"},
            "expires_at": {"type": "string", "format": "date-time"},
            "idle_expires_at": {"type": "string", "format": "date-time"},
            "current_session": {"type": "boolean"},
            "read_only": {"type": "boolean"},
        },
        "required": [
            "id",
            "platform",
            "device",
            "browser",
            "created_at",
            "last_activity_at",
            "expires_at",
            "idle_expires_at",
            "current_session",
            "read_only",
        ],
    },
    "SessionPageResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/AuthenticatedSession"},
            },
            "pagination": {"$ref": "#/components/schemas/Pagination"},
        },
        "required": ["success", "data", "pagination"],
    },
}
