"""Executable contract regressions for retry-sensitive money-IN routes."""

from __future__ import annotations

from core.openapi import build_schema


def _methods(path: dict) -> set[str]:
    return {method for method in path if method in {"get", "head", "post", "put", "patch", "delete"}}


def test_sale_creation_publishes_exact_principal_retry_contract_and_closed_dto():
    schema = build_schema(None)
    path = schema["paths"]["/api/v1/sales/"]
    assert _methods(path) == {"get", "head", "post"}
    operation = path["post"]
    assert operation["security"] == [
        {"sessionAuth": []},
        {"cookieSession": [], "csrfHeader": []},
    ]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert set(parameters) == {"Idempotency-Key"}
    assert parameters["Idempotency-Key"]["required"] is True
    assert parameters["Idempotency-Key"]["schema"] == {
        "type": "string",
        "minLength": 16,
        "maxLength": 128,
    }
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SaleCreateRequest"
    }
    request = schema["components"]["schemas"]["SaleCreateRequest"]
    assert request["additionalProperties"] is False
    assert set(request["properties"]) == {
        "item",
        "quantity",
        "unit_price_uzs",
        "student",
        "payment_method",
        "note",
    }
    assert set(operation["responses"]).issuperset(
        {"201", "400", "401", "402", "403", "404", "405", "409", "422", "429", "503"}
    )
    public_sale = schema["components"]["schemas"]["Sale"]
    assert {
        "idempotency_key",
        "idempotency_key_hash",
        "operation_fingerprint",
        "sold_by_principal_kind",
        "sold_by_principal_id",
        "creation_response_snapshot",
    }.isdisjoint(public_sale["properties"])


def test_loan_repayment_publishes_stable_snapshot_and_closed_retry_contract():
    schema = build_schema(None)
    path = schema["paths"]["/api/v1/loans/{pk}/repay/"]
    assert _methods(path) == {"post"}
    operation = path["post"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert set(parameters) == {"pk", "Idempotency-Key"}
    assert parameters["Idempotency-Key"]["required"] is True
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/LoanRepaymentCreateRequest"
    }
    request = schema["components"]["schemas"]["LoanRepaymentCreateRequest"]
    assert request["additionalProperties"] is False
    assert request["required"] == ["amount_uzs", "payment_method"]
    assert set(request["properties"]) == {"amount_uzs", "payment_method", "note"}
    response = operation["responses"]["201"]["content"]["application/json"]["schema"]
    assert response == {"$ref": "#/components/schemas/LoanRepaymentResultResponse"}
    result = schema["components"]["schemas"]["LoanRepaymentResult"]
    assert {"repaid_uzs", "outstanding_uzs", "settled"}.issubset(result["required"])
    assert {
        "idempotency_key",
        "idempotency_key_hash",
        "operation_fingerprint",
        "recorded_by_principal_kind",
        "recorded_by_principal_id",
        "response_snapshot",
        "repaid_after_uzs",
        "outstanding_after_uzs",
    }.isdisjoint(result["properties"])
    assert set(operation["responses"]).issuperset(
        {"201", "400", "401", "402", "403", "404", "405", "409", "422", "429", "503"}
    )
