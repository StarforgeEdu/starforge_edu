"""Explicit public contracts for organization mobile-support endpoints."""

from __future__ import annotations

from core.openapi_contracts import SESSION_SECURITY, OperationContract, error_response

_STAFF_APP_STATUS_RESPONSE = {
    "description": "Privacy-minimized feature availability for the active staff account.",
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
                            "features": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "feature": {
                                            "type": "string",
                                            "enum": [
                                                "ai",
                                                "notifications",
                                                "groups",
                                                "attendance",
                                                "library",
                                                "printing",
                                                "messaging",
                                                "tasks",
                                            ],
                                        },
                                        "status": {
                                            "type": "string",
                                            "enum": ["available", "degraded", "unavailable"],
                                        },
                                    },
                                    "required": ["feature", "status"],
                                },
                            }
                        },
                        "required": ["features"],
                    },
                },
                "required": ["success", "data"],
            }
        }
    },
}


STAFF_APP_STATUS_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Read staff-app feature availability",
    description=(
        "Returns only product feature names and normalized availability. It exposes no "
        "dependency topology, runtime warning text, disabled-policy list, infrastructure "
        "details, or mutation controls. Only role-native teacher and non-executive staff "
        "accounts accepted by the staff mobile application may read it."
    ),
    security=SESSION_SECURITY,
    responses={
        "200": _STAFF_APP_STATUS_RESPONSE,
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The active role account is not accepted by the staff application."),
        "429": error_response("Authenticated request rate limit exceeded."),
        "503": error_response("Feature policy is temporarily unavailable."),
    },
    operation_id="get_staff_app_feature_availability",
)


STAFF_APP_STATUS_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check staff-app feature availability access",
    description="Same authentication and authorization semantics as GET, with no response body.",
    security=SESSION_SECURITY,
    responses={
        "200": {"description": "Feature availability is readable by this staff account."},
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The active role account is not accepted by the staff application."),
        "429": error_response("Authenticated request rate limit exceeded."),
        "503": error_response("Feature policy is temporarily unavailable."),
    },
    operation_id="head_staff_app_feature_availability",
)
