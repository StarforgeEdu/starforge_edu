"""DB-free contracts for the BranchAgent device boundary and plain Django URLs."""

from __future__ import annotations

from django.urls import get_resolver

from core.openapi import (
    _is_public,
    _openapi_path,
    _view_source,
    _walk,
    build_schema,
)
from core.openapi_contracts import get_openapi_contract

_HTTP_OPERATIONS = {"get", "head", "post", "put", "patch", "delete"}


def test_branch_agent_operations_publish_exact_custom_auth_and_closed_dtos():
    schema = build_schema(None)
    paths = schema["paths"]
    schemes = schema["components"]["securitySchemes"]

    assert schemes["branchAgentAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "Authorization",
        "description": schemes["branchAgentAuth"]["description"],
    }
    assert "Authorization: Agent" in schemes["branchAgentAuth"]["description"]

    claim_item = paths["/api/v1/printing/agent/claim/"]
    assert set(claim_item).intersection(_HTTP_OPERATIONS) == {"post"}
    claim = claim_item["post"]
    assert claim["security"] == [{"branchAgentAuth": []}]
    assert claim["requestBody"]["required"] is False
    claim_request = claim["requestBody"]["content"]["application/json"]["schema"]
    assert claim_request["additionalProperties"] is False
    assert claim_request["maxProperties"] == 0
    assert set(claim["responses"]) == {
        "200",
        "204",
        "400",
        "401",
        "402",
        "409",
        "429",
        "503",
    }

    claim_response = claim["responses"]["200"]["content"]["application/json"]["schema"]
    agent_job = claim_response["properties"]["data"]["properties"]["job"]
    assert agent_job["additionalProperties"] is False
    assert {
        "branch",
        "agent",
        "source_id",
        "cohort_id",
        "requested_by",
        "payload_s3_key",
        "last_error",
        "created_at",
        "claimed_at",
        "finished_at",
    }.isdisjoint(agent_job["properties"])
    assert agent_job["properties"]["status"]["enum"] == ["picked"]
    assert agent_job["properties"]["lease_id"] == {"type": "string", "format": "uuid"}
    assert agent_job["properties"]["lease_expires_at"] == {
        "type": "string",
        "format": "date-time",
    }

    status_item = paths["/api/v1/printing/agent/jobs/{job_id}/status/"]
    assert set(status_item).intersection(_HTTP_OPERATIONS) == {"post"}
    status = status_item["post"]
    assert status["security"] == [{"branchAgentAuth": []}]
    status_request = status["requestBody"]["content"]["application/json"]["schema"]
    variants = {variant["properties"]["status"]["enum"][0]: variant for variant in status_request["oneOf"]}
    assert set(variants) == {"printing", "done", "failed"}
    assert all(variant["additionalProperties"] is False for variant in variants.values())
    assert all(variant["required"] == ["lease_id", "status"] for variant in variants.values())
    assert all(
        variant["properties"]["lease_id"] == {"type": "string", "format": "uuid"}
        for variant in variants.values()
    )
    assert "error" not in variants["printing"]["properties"]
    assert "error" not in variants["done"]["properties"]
    assert variants["failed"]["properties"]["error"]["maxLength"] == 2000
    assert all(variant["properties"]["pages_printed"]["minimum"] == 0 for variant in variants.values())
    assert status["parameters"] == [
        {
            "name": "job_id",
            "in": "path",
            "required": True,
            "schema": {"type": "integer", "format": "int64", "minimum": 1},
        }
    ]
    assert set(status["responses"]) == {
        "200",
        "400",
        "401",
        "402",
        "404",
        "409",
        "429",
        "503",
    }

    heartbeat_item = paths["/api/v1/printing/agent/jobs/{job_id}/heartbeat/"]
    assert set(heartbeat_item).intersection(_HTTP_OPERATIONS) == {"post"}
    heartbeat = heartbeat_item["post"]
    assert heartbeat["security"] == [{"branchAgentAuth": []}]
    heartbeat_request = heartbeat["requestBody"]["content"]["application/json"]["schema"]
    assert heartbeat_request["additionalProperties"] is False
    assert heartbeat_request["required"] == ["lease_id"]
    assert heartbeat_request["properties"] == {
        "lease_id": {"type": "string", "format": "uuid"},
        "pages_printed": {"type": "integer", "minimum": 0},
    }
    assert heartbeat["parameters"] == status["parameters"]
    assert set(heartbeat["responses"]) == {
        "200",
        "400",
        "401",
        "402",
        "404",
        "409",
        "429",
        "503",
    }


def test_every_tenant_plain_django_url_has_a_reviewed_auth_boundary():
    """A newly mounted FBV cannot silently become an unguarded tenant endpoint.

    DRF class callbacks enforce their own authentication classes and are covered by
    the DRF/schema suites. Plain callbacks must have the staff-session guard, the
    BranchAgent guard, or an exact reviewed public/optional declaration.
    """

    unprotected: list[str] = []
    for route, callback, _name in _walk(get_resolver(None).url_patterns, ""):
        if not route.startswith("api/v1/") or getattr(callback, "cls", None) is not None:
            continue
        path, _parameters = _openapi_path(route)
        source = _view_source(callback)
        header = source.split("\ndef ", 1)[0]
        if "require_auth" in header or "require_branch_agent" in header:
            continue

        contract = get_openapi_contract(callback)
        explicitly_anonymous = bool(
            contract
            and all(
                operation.security == () or any(not requirement for requirement in (operation.security or ()))
                for operation in contract.operations
            )
        )
        if _is_public(path) or explicitly_anonymous:
            continue
        unprotected.append(path)

    assert unprotected == []
