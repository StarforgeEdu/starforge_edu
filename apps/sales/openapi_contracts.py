"""Executable contracts for the retry-safe point-of-sale register."""

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

IDEMPOTENCY_HEADER = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": True,
    "schema": {"type": "string", "minLength": 16, "maxLength": 128},
    "description": ("Visible ASCII retry key. Only its tenant/role-principal-scoped SHA-256 hash is stored."),
}


def _query(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "in": "query", "required": False, "schema": schema}


SALE_LIST_PARAMETERS = (
    _query("page", {"type": "integer", "minimum": 1}),
    _query("page_size", {"type": "integer", "minimum": 1, "maximum": 100}),
    _query("status", {"type": "string", "enum": ["completed", "refunded"]}),
    _query("branch", {"type": "integer", "minimum": 1}),
    _query("student", {"type": "integer", "minimum": 1}),
    _query("payment_method", {"type": "integer", "minimum": 1}),
    _query(
        "ordering",
        {
            "type": "string",
            "enum": ["created_at", "-created_at", "amount_uzs", "-amount_uzs"],
        },
    ),
)


def _read_errors() -> dict[str, Any]:
    return {
        "400": error_response("A list filter is invalid."),
        "401": error_response("The session is absent, invalid, expired, or revoked."),
        "403": error_response("The active role principal lacks sale read authority."),
        "405": error_response("The HTTP method is not supported by this operation path."),
        "429": error_response("The authenticated request rate limit was exceeded."),
    }


SALES_COLLECTION_CONTRACTS = (
    OperationContract(
        method="GET",
        summary="List sales in the current till scope",
        description=("Returns only sales inside the exact branch memberships carrying sale:read."),
        permission="sale:read",
        security=SESSION_SECURITY,
        parameters=SALE_LIST_PARAMETERS,
        responses={"200": json_response("Scoped sales page.", "SalePageResponse"), **_read_errors()},
        operation_id="get_sales",
    ),
    OperationContract(
        method="HEAD",
        summary="Check sales-register visibility",
        description="Performs the same authentication, authorization, and scope checks as GET.",
        permission="sale:read",
        security=SESSION_SECURITY,
        parameters=SALE_LIST_PARAMETERS,
        responses={"200": {"description": "The scoped sales register is visible."}, **_read_errors()},
        operation_id="head_sales",
    ),
    OperationContract(
        method="POST",
        summary="Record one point-of-sale purchase",
        description=(
            "Creates one sale and one immutable money-IN ledger row. The mandatory retry key is "
            "scoped to the exact tenant and authenticated role principal. An exact retry returns "
            "the original sale; changed reuse returns 409. Current permission is rechecked before "
            "every replay. A retry is authorized against the historical sale branch; a new sale "
            "is authorized against the student's locked current branch."
        ),
        permission="sale:write",
        security=UNSAFE_SESSION_SECURITY,
        parameters=(IDEMPOTENCY_HEADER,),
        request_body=json_request("SaleCreateRequest"),
        responses={
            "201": json_response("The original or replayed sale.", "SaleResponse"),
            "400": error_response("The JSON body or idempotency key is invalid."),
            "401": error_response("The session is absent, invalid, expired, or revoked."),
            "403": error_response("The active role principal lacks sale write authority in this branch."),
            "404": error_response(
                "The student is unavailable for a new sale, or the historical sale is outside "
                "current sale-write scope on retry."
            ),
            "405": error_response("The HTTP method is not supported by this operation path."),
            "409": error_response("The idempotency key belongs to a different sale operation."),
            "422": error_response("The payment method or sale transition is not valid."),
            "429": error_response("The authenticated request rate limit was exceeded."),
        },
        operation_id="post_sales",
    ),
)


OPENAPI_SCHEMAS: dict[str, dict[str, Any]] = {
    "SaleCreateRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "item": {"type": "string", "minLength": 1, "maxLength": 200},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 1_000_000, "default": 1},
            "unit_price_uzs": {
                "type": "string",
                "pattern": r"^\d{1,16}(?:\.\d{1,2})?$",
                "description": "Positive decimal-major UZS; never send a JSON float.",
            },
            "student": {"type": "integer", "minimum": 1},
            "payment_method": {"type": "integer", "minimum": 1},
            "note": {"type": "string", "maxLength": 255},
        },
        "required": ["item", "unit_price_uzs", "student", "payment_method"],
    },
    "Sale": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "item": {"type": "string"},
            "quantity": {"type": "integer", "minimum": 1},
            "unit_price_uzs": {"type": "string", "pattern": r"^\d+\.\d{2}$"},
            "amount_uzs": {"type": "string", "pattern": r"^\d+\.\d{2}$"},
            "student": {"type": "integer", "minimum": 1},
            "branch": {"type": "integer", "nullable": True},
            "payment_method": {"type": "integer", "nullable": True},
            "status": {"type": "string", "enum": ["completed", "refunded"]},
            "ledger_entry": {"type": "integer", "nullable": True},
            "refund_ledger_entry": {"type": "integer", "nullable": True},
            "sold_by": {"type": "integer", "nullable": True},
            "refunded_by": {"type": "integer", "nullable": True},
            "refunded_at": {"type": "string", "format": "date-time", "nullable": True},
            "refund_reason": {"type": "string"},
            "note": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "required": [
            "id",
            "item",
            "quantity",
            "unit_price_uzs",
            "amount_uzs",
            "student",
            "branch",
            "payment_method",
            "status",
            "ledger_entry",
            "refund_ledger_entry",
            "sold_by",
            "refunded_by",
            "refunded_at",
            "refund_reason",
            "note",
            "created_at",
        ],
    },
    "SaleResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"$ref": "#/components/schemas/Sale"},
        },
        "required": ["success", "data"],
    },
    "SalePageResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"type": "array", "items": {"$ref": "#/components/schemas/Sale"}},
            "pagination": {"$ref": "#/components/schemas/Pagination"},
        },
        "required": ["success", "data", "pagination"],
    },
}
