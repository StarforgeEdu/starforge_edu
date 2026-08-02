"""Explicit OpenAPI operation metadata for the critical authentication lifecycle."""

from core.openapi_contracts import (
    OPTIONAL_LOGOUT_SECURITY,
    PUBLIC_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)

SESSION_BOOTSTRAP_CONTRACT = OperationContract(
    method="GET",
    summary="Bootstrap browser CSRF protection",
    description=(
        "Sets the CSRF cookie and returns its masked token. This does not authenticate "
        "the caller and never returns a session credential."
    ),
    security=PUBLIC_SECURITY,
    responses={
        "200": json_response("CSRF bootstrap created.", "SessionBootstrapResponse"),
        "429": error_response("Anonymous request rate limit exceeded."),
    },
    operation_id="get_auth_session_bootstrap",
)

ROLE_LOGIN_CONTRACT = OperationContract(
    method="POST",
    summary="Sign in with a role-native account",
    description=(
        "Canonical sign-in for student, teacher, parent, and staff accounts. The request "
        "Host selects the tenant. `role=staff` is only a transport principal kind; management "
        "clients must bootstrap `/api/v1/users/me/` and inspect active memberships. Send "
        "`X-Session-Transport: cookie` with a token from the session bootstrap to keep the "
        "opaque credential exclusively in a Secure HttpOnly cookie."
    ),
    security=PUBLIC_SECURITY,
    parameters=(
        {
            "name": "X-Session-Transport",
            "in": "header",
            "required": False,
            "schema": {"type": "string", "enum": ["cookie"]},
            "description": "Request HttpOnly cookie transport; omit for a bearer response.",
        },
        {
            "name": "X-CSRFToken",
            "in": "header",
            "required": False,
            "schema": {"type": "string"},
            "description": "Required only when X-Session-Transport is cookie.",
        },
    ),
    request_body=json_request("RoleLoginRequest"),
    responses={
        "200": json_response(
            "Authenticated; returns a bearer key or sets an HttpOnly cookie.",
            "RoleLoginResponse",
        ),
        "400": error_response("Malformed JSON or invalid field type."),
        "401": error_response("Username/password pair is invalid."),
        "403": error_response("Cookie transport CSRF validation failed."),
        "404": error_response("The request Host does not resolve to an active tenant."),
        "422": error_response("Required username or password is missing."),
        "429": error_response("Per-IP or per-username login rate limit exceeded."),
    },
    operation_id="post_auth_role_login",
)

LOGOUT_CONTRACT = OperationContract(
    method="POST",
    summary="Sign out the current session",
    description=(
        "Idempotently revokes only the credential used by this request. Missing, expired, "
        "malformed, or previously revoked credentials are already signed out and still succeed."
    ),
    security=OPTIONAL_LOGOUT_SECURITY,
    responses={
        "200": json_response("The current session is signed out.", "LogoutResponse"),
        "403": error_response("Cookie CSRF validation failed."),
        "429": error_response("Request rate limit exceeded."),
    },
    operation_id="post_auth_logout",
)

LOGOUT_ALL_CONTRACT = OperationContract(
    method="POST",
    summary="Sign out every session",
    description=(
        "Idempotently revokes every active session for the current principal. An absent or "
        "expired credential is already signed out and succeeds; read-only sessions fail closed."
    ),
    security=OPTIONAL_LOGOUT_SECURITY,
    responses={
        "200": json_response("All sessions are signed out.", "LogoutResponse"),
        "403": error_response("Cookie CSRF validation failed or the session is read-only."),
        "429": error_response("Request rate limit exceeded."),
    },
    operation_id="post_auth_logout_all",
)

PASSWORD_CHANGE_CONTRACT = OperationContract(
    method="POST",
    summary="Change the current principal password",
    description=(
        "Validates the raw new password with every configured Django password validator, "
        "clears mandatory-password state, revokes prior sessions, and issues one replacement "
        "credential for the current device."
    ),
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("PasswordChangeRequest"),
    responses={
        "200": json_response(
            "Password changed and replacement session issued.",
            "PasswordChangeResponse",
        ),
        "400": error_response(
            "Current password is wrong, new password is weak, or JSON is malformed.",
            "PasswordChangeError",
        ),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("Cookie CSRF validation failed or the session is read-only."),
        "429": error_response("Request rate limit exceeded."),
    },
    operation_id="post_auth_password_change",
)
