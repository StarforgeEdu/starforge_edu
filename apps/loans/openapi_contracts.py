"""Executable contract for retry-safe staff-loan repayments."""

from __future__ import annotations

from typing import Any

from core.openapi_contracts import (
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)

IDEMPOTENCY_HEADER = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": True,
    "schema": {"type": "string", "minLength": 16, "maxLength": 128},
    "description": (
        "Visible ASCII retry key. Only its tenant/role-principal-scoped SHA-256 hash is stored."
    ),
}
LOAN_ID_PARAMETER = {
    "name": "pk",
    "in": "path",
    "required": True,
    "schema": {"type": "integer", "minimum": 1},
    "description": "Staff-loan approval identifier resolved inside current loan:collect scope.",
}

LOAN_REPAY_CONTRACTS = (
    OperationContract(
        method="POST",
        summary="Record one staff-loan repayment",
        description=(
            "Creates one repayment and one immutable money-IN ledger row. The mandatory retry "
            "key is scoped to the exact tenant and authenticated role principal. An exact retry "
            "returns the original post-operation loan snapshot, even after later repayments; "
            "changed reuse returns 409. Current collect authority and loan scope are checked "
            "before every replay."
        ),
        permission="loan:collect",
        security=UNSAFE_SESSION_SECURITY,
        parameters=(LOAN_ID_PARAMETER, IDEMPOTENCY_HEADER),
        request_body=json_request("LoanRepaymentCreateRequest"),
        responses={
            "201": json_response(
                "The immutable post-operation loan snapshot.",
                "LoanRepaymentResultResponse",
            ),
            "400": error_response("The JSON body or idempotency key is invalid."),
            "401": error_response("The session is absent, invalid, expired, or revoked."),
            "403": error_response("The active role principal lacks loan collection authority."),
            "404": error_response("The loan is absent or outside current collection scope."),
            "405": error_response("The HTTP method is not supported by this operation path."),
            "409": error_response("The idempotency key belongs to a different repayment."),
            "422": error_response("The repayment exceeds or conflicts with the loan state."),
            "429": error_response("The authenticated request rate limit was exceeded."),
        },
        operation_id="post_loan_repayment",
    ),
)


OPENAPI_SCHEMAS: dict[str, dict[str, Any]] = {
    "LoanRepaymentCreateRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "amount_uzs": {
                "type": "string",
                "pattern": r"^\d{1,16}(?:\.\d{1,2})?$",
                "description": "Positive decimal-major UZS; never send a JSON float.",
            },
            "payment_method": {"type": "integer", "minimum": 1},
            "note": {"type": "string", "maxLength": 255},
        },
        "required": ["amount_uzs", "payment_method"],
    },
    "LoanRepaymentResult": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "kind": {"type": "string", "enum": ["loan"]},
            "branch": {"type": "integer", "nullable": True},
            "requested_by": {"type": "integer", "nullable": True},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "amount_uzs": {"type": "string", "pattern": r"^\d+\.\d{2}$", "nullable": True},
            "payload": {"type": "object"},
            "status": {"type": "string", "enum": ["disbursed"]},
            "decided_by": {"type": "integer", "nullable": True},
            "decided_at": {"type": "string", "format": "date-time", "nullable": True},
            "disbursed_by": {"type": "integer", "nullable": True},
            "disbursed_at": {"type": "string", "format": "date-time", "nullable": True},
            "ledger_entry": {"type": "integer", "nullable": True},
            "repaid_uzs": {"type": "string", "pattern": r"^\d+\.\d{2}$"},
            "outstanding_uzs": {"type": "string", "pattern": r"^\d+\.\d{2}$"},
            "settled": {"type": "boolean"},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "required": [
            "id",
            "kind",
            "branch",
            "requested_by",
            "title",
            "description",
            "amount_uzs",
            "payload",
            "status",
            "decided_by",
            "decided_at",
            "disbursed_by",
            "disbursed_at",
            "ledger_entry",
            "repaid_uzs",
            "outstanding_uzs",
            "settled",
            "created_at",
        ],
    },
    "LoanRepaymentResultResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"$ref": "#/components/schemas/LoanRepaymentResult"},
        },
        "required": ["success", "data"],
    },
}
