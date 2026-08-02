"""Executable contract for durable statement-of-account exports."""

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


def _errors(*, unprocessable: bool = False) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "400": error_response("The request DTO or identifier is invalid."),
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "402": error_response("The tenant subscription does not include finance."),
        "403": error_response("The active role principal lacks finance read authority."),
        "404": error_response("The student/export is absent or outside the exact live scope."),
        "405": error_response("The HTTP method is not supported by this operation path."),
        "429": error_response("The statement render queue or request rate limit was exceeded."),
        "503": error_response("The durable job could not be published or storage is unavailable."),
    }
    if unprocessable:
        responses["422"] = error_response("The authorized statement exceeds the bounded export size.")
    return responses


STATEMENT_REQUEST_OPERATION = OperationContract(
    method="POST",
    summary="Request a durable student statement PDF",
    description=(
        "Creates a tenant-local export row and immutable invoice links before publishing background "
        "work. The returned task_id is a compatibility alias for export_id; neither value identifies "
        "a Celery result. Identical active snapshots are safely coalesced."
    ),
    permission="finance:read",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("StatementExportRequest"),
    responses={
        "202": json_response(
            "The durable export was accepted or safely coalesced.", "StatementExportResponse"
        ),
        **_errors(unprocessable=True),
    },
    operation_id="post_finance_statement_export",
)

_EXPORT_ID_PARAMETER = {
    "name": "export_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "format": "uuid"},
    "description": "Tenant-local durable export identifier returned by the request operation.",
}

STATEMENT_RESULT_OPERATIONS = (
    OperationContract(
        method="GET",
        summary="Read durable statement status or obtain a short-lived download URL",
        description=(
            "Revalidates the exact creator role principal and every immutable invoice link against "
            "current grants and branch/department scope before returning status or signing the "
            "deterministic private object. Out-of-scope jobs return 404."
        ),
        permission="finance:read",
        security=SESSION_SECURITY,
        parameters=(_EXPORT_ID_PARAMETER,),
        responses={
            "200": json_response("Current durable export state.", "StatementExportResponse"),
            **_errors(),
        },
        operation_id="get_finance_statement_export",
    ),
    OperationContract(
        method="HEAD",
        summary="Check durable statement visibility",
        description="Performs the same owner, role-principal, permission, and scope checks as GET.",
        permission="finance:read",
        security=SESSION_SECURITY,
        parameters=(_EXPORT_ID_PARAMETER,),
        responses={"200": {"description": "The export is visible."}, **_errors()},
        operation_id="head_finance_statement_export",
    ),
)


OPENAPI_SCHEMAS: dict[str, dict[str, Any]] = {
    "StatementExportRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "locale": {"type": "string", "enum": ["en", "ru", "uz"], "default": "en"},
        },
    },
    "StatementExportStatus": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_id": {"type": "string", "format": "uuid", "deprecated": True},
            "export_id": {"type": "string", "format": "uuid"},
            "status": {
                "type": "string",
                "enum": ["pending", "done", "failed", "expired"],
            },
            "state": {
                "type": "string",
                "enum": ["queued", "running", "done", "failed", "expired"],
            },
            "url": {"type": "string", "format": "uri", "nullable": True},
            "error_code": {
                "type": "string",
                "enum": ["statement_generation_failed"],
                "nullable": True,
            },
            "created_at": {"type": "string", "format": "date-time"},
            "expires_at": {"type": "string", "format": "date-time"},
        },
        "required": [
            "task_id",
            "export_id",
            "status",
            "state",
            "url",
            "error_code",
            "created_at",
            "expires_at",
        ],
    },
    "StatementExportResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"$ref": "#/components/schemas/StatementExportStatus"},
        },
        "required": ["success", "data"],
    },
}
