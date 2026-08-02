"""Explicit contract for the fiscal-receipt observation and generation workflow."""

from __future__ import annotations

from typing import Any

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
)


def _success_envelope(*variants: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["success", "data"],
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"oneOf": list(variants)},
        },
    }


_URL_DATA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url"],
    "properties": {"url": {"type": "string", "format": "uri"}},
}
_PENDING_DATA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status"],
    "properties": {"status": {"type": "string", "enum": ["pending"]}},
}
_NOT_GENERATED_DATA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "generation_required"],
    "properties": {
        "status": {"type": "string", "enum": ["not_generated"]},
        "generation_required": {"type": "boolean", "enum": [True]},
    },
}
_GENERATING_DATA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status"],
    "properties": {"status": {"type": "string", "enum": ["generating"]}},
}


def _json_response(description: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"description": description}
    if schema is not None:
        response["content"] = {"application/json": {"schema": schema}}
    return response


_COMMON_ERRORS = {
    "401": error_response("Session is absent, invalid, expired, or revoked."),
    "403": error_response("The principal lacks payment read authority."),
    "404": error_response("The payment, its scope, or its fiscal receipt is unavailable."),
    "429": error_response("Authenticated request rate limit exceeded."),
}


PAYMENT_RECEIPT_CONTRACTS = (
    OperationContract(
        method="GET",
        summary="Observe fiscal-receipt readiness",
        description=(
            "Returns a short-lived download URL when the PDF exists, otherwise reports fiscal "
            "or PDF readiness. GET is strictly observational and never queues rendering or "
            "writes storage. Use POST to request missing PDF generation explicitly."
        ),
        permission="payments:read",
        security=SESSION_SECURITY,
        responses={
            "200": _json_response(
                "The receipt PDF is ready, or explicit generation is required.",
                _success_envelope(_URL_DATA, _NOT_GENERATED_DATA),
            ),
            "202": _json_response(
                "Fiscal confirmation is still pending.",
                _success_envelope(_PENDING_DATA),
            ),
            **_COMMON_ERRORS,
        },
        operation_id="get_payment_receipt",
    ),
    OperationContract(
        method="HEAD",
        summary="Check fiscal-receipt readiness",
        description=(
            "Uses the same authorization, state, and status semantics as GET without a response "
            "body. HEAD is strictly observational and never queues rendering or writes storage."
        ),
        permission="payments:read",
        security=SESSION_SECURITY,
        responses={
            "200": _json_response("The PDF is ready or can be generated."),
            "202": _json_response("Fiscal confirmation is still pending."),
            **_COMMON_ERRORS,
        },
        operation_id="head_payment_receipt",
    ),
    OperationContract(
        method="POST",
        summary="Generate a missing fiscal-receipt PDF",
        description=(
            "Idempotently queues PDF rendering only for a confirmed fiscal receipt whose trusted "
            "PDF object is absent. Existing PDFs are returned directly and unconfirmed receipts "
            "remain pending. The request has no JSON body."
        ),
        permission="payments:read",
        security=UNSAFE_SESSION_SECURITY,
        responses={
            "200": _json_response(
                "The receipt PDF already exists.",
                _success_envelope(_URL_DATA),
            ),
            "202": _json_response(
                "Fiscal confirmation is pending or PDF generation was queued.",
                _success_envelope(_PENDING_DATA, _GENERATING_DATA),
            ),
            **_COMMON_ERRORS,
        },
        operation_id="post_payment_receipt_generation",
    ),
)
