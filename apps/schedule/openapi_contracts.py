"""Explicit contracts for the signed personal iCalendar lifecycle."""

from __future__ import annotations

from typing import Any

from core.openapi_contracts import (
    PUBLIC_SECURITY,
    SESSION_SECURITY,
    OperationContract,
    error_response,
)


def _calendar_url_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
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
                                "url": {"type": "string", "format": "uri"},
                            },
                            "required": ["url"],
                        },
                    },
                    "required": ["success", "data"],
                }
            }
        },
    }


def _calendar_response(description: str, *, include_body: bool) -> dict[str, Any]:
    response: dict[str, Any] = {"description": description}
    if include_body:
        response["content"] = {
            "text/calendar": {
                "schema": {"type": "string", "format": "binary"},
            }
        }
    return response


ICAL_URL_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Issue the current principal's signed calendar-feed URL",
    description=(
        "Returns a tenant-bound, revocable URL credential for the caller's scoped lesson "
        "feed. Treat the URL as a secret: do not place it in application logs or analytics."
    ),
    security=SESSION_SECURITY,
    responses={
        "200": _calendar_url_response("Signed personal calendar URL issued."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "405": error_response("HTTP method is not supported."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="get_schedule_ical_url",
)

ICAL_URL_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check calendar-feed URL issuance",
    description="Same authentication and status semantics as GET, with no response body.",
    security=SESSION_SECURITY,
    responses={
        "200": {"description": "The current session may issue a calendar URL."},
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "405": error_response("HTTP method is not supported."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="head_schedule_ical_url",
)

ICAL_FEED_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Read a signed personal calendar feed",
    description=(
        "The signed tenant-bound path token is the credential; no browser session is used. "
        "Invalid, expired, cross-tenant, deactivated-account, and revoked tokens fail closed."
    ),
    security=PUBLIC_SECURITY,
    responses={
        "200": _calendar_response("Personal iCalendar document.", include_body=True),
        "401": error_response("Feed token is invalid, expired, revoked, or belongs to another tenant."),
        "405": error_response("HTTP method is not supported."),
        "429": error_response("Public feed request rate limit exceeded."),
    },
    operation_id="get_schedule_ical_feed",
)

ICAL_FEED_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check a signed personal calendar feed",
    description="Same token validation and status semantics as GET, with no response body.",
    security=PUBLIC_SECURITY,
    responses={
        "200": _calendar_response("The signed calendar feed is available.", include_body=False),
        "401": error_response("Feed token is invalid, expired, revoked, or belongs to another tenant."),
        "405": error_response("HTTP method is not supported."),
        "429": error_response("Public feed request rate limit exceeded."),
    },
    operation_id="head_schedule_ical_feed",
)
