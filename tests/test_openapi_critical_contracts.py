"""Executable regressions for security- and product-critical OpenAPI routes.

These tests do not touch the database.  Schema construction walks the real Django
URL resolver, and the builder independently verifies every registered callback's
runtime method and authentication guards before it accepts explicit metadata.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from django.test import RequestFactory
from django.urls import get_resolver, resolve

from core.openapi import (
    _components,
    _openapi_path,
    _runtime_methods,
    _validate_view_contract,
    _walk,
    build_schema,
)
from core.openapi_contracts import (
    PUBLIC_SECURITY,
    OpenAPIContractError,
    OperationContract,
    get_openapi_contract,
    json_response,
    openapi_contract,
)

HTTP_OPERATIONS = {"get", "head", "post", "put", "patch", "delete"}
AUTH_RUNTIME_RESPONSES = {"503"}
TENANT_RUNTIME_RESPONSES = {"402", "503"}


def _operation_methods(path_item: dict) -> set[str]:
    return set(path_item) & HTTP_OPERATIONS


def _response_component(operation: dict, status: str) -> str:
    reference = operation["responses"][status]["content"]["application/json"]["schema"]["$ref"]
    return reference.rsplit("/", 1)[-1]


def test_every_registered_critical_contract_matches_its_runtime_url_and_methods():
    registered: dict[str, tuple[set[str], set[str]]] = {}
    for route, callback, name in _walk(get_resolver(None).url_patterns, ""):
        contract = get_openapi_contract(callback)
        if contract is None:
            continue
        runtime_path, _parameters = _openapi_path(route)
        _validate_view_contract(
            contract=contract,
            callback=callback,
            path=runtime_path,
            name=name,
        )
        registered[runtime_path] = (
            {operation.method for operation in contract.operations},
            set(_runtime_methods(callback)),
        )

    assert set(registered) >= {
        "/api/v1/auth/session/",
        "/api/v1/auth/role-login/",
        "/api/v1/auth/logout/",
        "/api/v1/auth/logout-all/",
        "/api/v1/auth/password/change/",
        "/api/v1/users/me/",
        "/api/v1/users/sessions/",
        "/api/v1/users/sessions/{pk}/",
        "/api/v1/students/{pk}/leadership-profile/",
        "/api/v1/audit/",
        "/api/v1/audit/{pk}/",
        "/api/v1/audit/export/",
        "/api/v1/achievements/{pk}/approve/",
        "/api/v1/achievements/{pk}/reject/",
        "/api/v1/intelligence/executive-summary/",
        "/api/v1/cards/wallets/me/",
        "/api/v1/cards/wallets/{student_id}/",
        "/api/v1/payments/{pk}/receipt/",
        "/api/v1/schedule/ical-url/",
        "/api/v1/schedule/ical/{token}/",
        "/api/v1/printing/jobs/",
        "/api/v1/printing/jobs/{pk}/reconciliations/",
        "/api/v1/printing/jobs/{pk}/reconcile/",
        "/api/v1/printing/agent/claim/",
        "/api/v1/printing/agent/jobs/{job_id}/heartbeat/",
        "/api/v1/printing/agent/jobs/{job_id}/status/",
        "/api/v1/teachers/{pk}/payout-policy/",
        "/api/v1/teachers/{pk}/prepare-salary/",
    }
    assert all(contracted == runtime for contracted, runtime in registered.values())


def test_session_register_and_student_leadership_profile_are_executable_contracts():
    schema = build_schema(None)

    sessions = schema["paths"]["/api/v1/users/sessions/"]
    assert _operation_methods(sessions) == {"get", "head"}
    assert sessions["get"]["security"] == [{"sessionAuth": []}, {"cookieSession": []}]
    assert _response_component(sessions["get"], "200") == "SessionPageResponse"
    assert {parameter["name"] for parameter in sessions["get"]["parameters"]} == {
        "page",
        "page_size",
    }
    session_delete = schema["paths"]["/api/v1/users/sessions/{pk}/"]["delete"]
    assert set(session_delete["responses"]) >= {"204", "401", "403", "404", "429"}
    assert session_delete["security"] == [
        {"sessionAuth": []},
        {"cookieSession": [], "csrfHeader": []},
    ]

    profile = schema["paths"]["/api/v1/students/{pk}/leadership-profile/"]
    assert _operation_methods(profile) == {"get", "head"}
    operation = profile["get"]
    assert "Requires permission `students:read`." in operation["description"]
    assert _response_component(operation, "200") == "StudentLeadershipProfileResponse"
    parameter_names = {parameter["name"] for parameter in [*profile["parameters"], *operation["parameters"]]}
    assert parameter_names == {
        "pk",
        "date_from",
        "date_to",
    }
    assert set(operation["responses"]) >= {"200", "400", "401", "403", "404", "429", "503"}

    session_row = schema["components"]["schemas"]["AuthenticatedSession"]
    assert session_row["additionalProperties"] is False
    assert {"key", "key_hash", "ip", "user_agent", "device_id"}.isdisjoint(session_row["properties"])
    profile_data = schema["components"]["schemas"]["StudentLeadershipProfileData"]
    assert profile_data["additionalProperties"] is False
    assert set(profile_data["required"]) >= {
        "generated_at",
        "window",
        "identity",
        "coverage",
        "warnings",
    }

    audit = schema["paths"]["/api/v1/audit/"]
    assert _operation_methods(audit) == {"get", "head"}
    audit_get = audit["get"]
    assert _response_component(audit_get, "200") == "AuditCursorPage"
    assert {parameter["name"] for parameter in audit_get["parameters"]} == {
        "actor",
        "actor_principal_kind",
        "actor_principal_id",
        "action",
        "resource_type",
        "resource_id",
        "ts_from",
        "ts_to",
        "branch",
        "department",
        "scope_status",
        "sensitivity",
        "cursor",
        "page_size",
    }
    action_parameter = next(
        parameter for parameter in audit_get["parameters"] if parameter["name"] == "action"
    )
    assert {
        "session.revoked",
        "print.job_reconciliation_required",
        "export.complete",
        "export.failed",
    } <= set(action_parameter["schema"]["enum"])
    detail = schema["paths"]["/api/v1/audit/{pk}/"]
    assert _operation_methods(detail) == {"get", "head"}
    assert _response_component(detail["get"], "200") == "AuditDetailResponse"
    export = schema["paths"]["/api/v1/audit/export/"]
    assert _operation_methods(export) == {"get", "head"}
    assert "text/csv" in export["get"]["responses"]["200"]["content"]

    actor_principal = schema["components"]["schemas"]["AuditActorPrincipal"]
    assert actor_principal["additionalProperties"] is False
    assert actor_principal["required"] == ["status", "kind", "id"]
    assert actor_principal["properties"]["status"]["enum"] == [
        "exact",
        "system",
        "unresolved",
    ]


def test_role_login_is_public_and_declares_exact_request_success_and_errors():
    schema = build_schema(None)
    item = schema["paths"]["/api/v1/auth/role-login/"]
    assert _operation_methods(item) == {"post"}
    operation = item["post"]
    assert "security" not in operation
    assert operation["requestBody"] == {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RoleLoginRequest"}}},
    }
    assert (
        set(operation["responses"])
        == {
            "200",
            "400",
            "401",
            "403",
            "404",
            "422",
            "429",
        }
        | AUTH_RUNTIME_RESPONSES
    )
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RoleLoginResponse"
    }

    request_schema = schema["components"]["schemas"]["RoleLoginRequest"]
    assert request_schema["required"] == ["username", "password"]
    assert {"username", "password", "device_id", "platform"} == set(request_schema["properties"])
    response_data = schema["components"]["schemas"]["RoleLoginData"]
    assert response_data["required"] == ["role", "must_change_password"]
    assert response_data["properties"]["role"]["enum"] == [
        "student",
        "teacher",
        "parent",
        "staff",
    ]


def test_users_me_is_the_typed_authorization_and_session_bootstrap():
    schema = build_schema(None)
    item = schema["paths"]["/api/v1/users/me/"]
    assert _operation_methods(item) == {"get", "head", "patch"}
    assert item["get"]["security"] == [{"sessionAuth": []}, {"cookieSession": []}]
    assert item["patch"]["security"] == [
        {"sessionAuth": []},
        {"cookieSession": [], "csrfHeader": []},
    ]
    assert item["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UserBootstrapResponse"
    }

    data = schema["components"]["schemas"]["UserBootstrapData"]
    assert data["additionalProperties"] is False
    assert {
        "principal_kind",
        "preferred_language",
        "role_memberships",
        "effective_permissions",
        "scopes",
        "organization_locale",
        "organization_timezone",
        "primary_currency",
        "read_only_session",
        "session_id",
        "session_created_at",
        "session_last_activity_at",
        "session_expires_at",
        "session_idle_expires_at",
        "server_time",
    } <= set(data["required"])
    update = schema["components"]["schemas"]["UserProfileUpdateRequest"]
    assert update["additionalProperties"] is False
    assert update["properties"]["preferred_language"]["enum"] == ["uz", "ru", "en"]
    scope = schema["components"]["schemas"]["PermissionScope"]
    assert scope["required"] == ["branch", "department", "effective_permissions"]


def test_session_logout_and_password_change_contracts_match_runtime_semantics():
    paths = build_schema(None)["paths"]
    session = paths["/api/v1/auth/session/"]
    assert _operation_methods(session) == {"get"}
    assert "security" not in session["get"]
    assert session["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SessionBootstrapResponse"
    }

    for path in ("/api/v1/auth/logout/", "/api/v1/auth/logout-all/"):
        operation = paths[path]["post"]
        assert _operation_methods(paths[path]) == {"post"}
        assert operation["security"][0] == {}
        assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/LogoutResponse"
        }

    change = paths["/api/v1/auth/password/change/"]["post"]
    assert change["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PasswordChangeRequest"
    }
    assert (
        set(change["responses"])
        == {
            "200",
            "400",
            "401",
            "403",
            "429",
        }
        | AUTH_RUNTIME_RESPONSES
    )
    assert change["responses"]["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PasswordChangeError"
    }


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_achievement_decisions_are_post_only_in_schema_and_runtime(action: str):
    path = f"/api/v1/achievements/{{pk}}/{action}/"
    item = build_schema(None)["paths"][path]
    assert _operation_methods(item) == {"post"}
    assert "requestBody" not in item["post"]
    assert (
        set(item["post"]["responses"])
        == {
            "200",
            "401",
            "403",
            "404",
            "422",
            "429",
        }
        | TENANT_RUNTIME_RESPONSES
    )

    callback = resolve(f"/api/v1/achievements/1/{action}/").func
    response = callback(RequestFactory().get(f"/api/v1/achievements/1/{action}/"), pk=1)
    assert response.status_code == 405


def test_executive_summary_publishes_only_the_closed_query_contract():
    schema = build_schema(None)
    item = schema["paths"]["/api/v1/intelligence/executive-summary/"]
    assert _operation_methods(item) == {"get", "head"}
    for method in ("get", "head"):
        parameters = item[method]["parameters"]
        assert {p["name"] for p in parameters if p["in"] == "query"} == {
            "branch",
            "department",
            "date_from",
            "date_to",
        }
        assert {p["name"] for p in parameters if p["in"] == "header"} == {"If-None-Match"}
        assert "requestBody" not in item[method]
        assert "page" not in {p["name"] for p in parameters}
        assert "search" not in {p["name"] for p in parameters}

    get_response = item["get"]["responses"]["200"]
    assert get_response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ExecutiveSummaryResponse"
    }
    assert set(get_response["headers"]) == {"ETag", "Cache-Control", "Vary"}
    assert "content" not in item["head"]["responses"]["200"]
    assert "content" not in item["get"]["responses"]["304"]

    components = schema["components"]["schemas"]
    wrapper = components["ExecutiveSummaryResponse"]
    assert wrapper["required"] == ["success", "data"]
    assert wrapper["properties"]["data"] == {"$ref": "#/components/schemas/ExecutiveSummaryData"}

    payload = components["ExecutiveSummaryData"]
    assert set(payload["required"]) == {
        "generated_at",
        "locale",
        "currency",
        "window",
        "scope",
        "coverage",
        "warnings",
    }
    assert set(payload["properties"]) == {
        *payload["required"],
        "students",
        "attendance",
        "retention",
        "capacity",
        "risk",
        "teachers",
        "branches",
        "finance",
        "attention",
    }
    assert not {
        "students",
        "attendance",
        "retention",
        "capacity",
        "risk",
        "teachers",
        "branches",
        "finance",
        "attention",
    } & set(payload["required"])
    assert components["ExecutiveCoverage"]["required"] == [
        "students",
        "attendance",
        "retention",
        "capacity",
        "risk",
        "teachers",
        "finance",
        "approvals",
        "tasks",
        "notifications",
        "meetings",
    ]
    assert components["ExecutiveWarning"]["required"] == [
        "code",
        "message",
        "affected_sections",
    ]


@pytest.mark.parametrize(
    "path",
    ["/api/v1/cards/wallets/me/", "/api/v1/cards/wallets/{student_id}/"],
)
def test_wallet_reads_are_documented_as_observational_get_and_head(path: str):
    item = build_schema(None)["paths"][path]
    assert _operation_methods(item) == {"get", "head"}
    assert "requestBody" not in item["get"]
    assert "requestBody" not in item["head"]
    assert "never provision a wallet or write any row" in item["get"]["description"]
    assert item["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WalletPayloadResponse"
    }


def test_receipt_read_methods_are_observational_and_generation_is_explicit_post():
    item = build_schema(None)["paths"]["/api/v1/payments/{pk}/receipt/"]
    assert _operation_methods(item) == {"get", "head", "post"}
    assert "requestBody" not in item["post"]
    assert "never queues rendering or writes storage" in item["get"]["description"]
    assert "never queues rendering or writes storage" in item["head"]["description"]
    assert item["get"]["security"] == [{"sessionAuth": []}, {"cookieSession": []}]
    assert item["post"]["security"] == [
        {"sessionAuth": []},
        {"cookieSession": [], "csrfHeader": []},
    ]
    assert set(item["get"]["responses"]) == {
        "200",
        "202",
        "401",
        "402",
        "403",
        "404",
        "429",
        "503",
    }
    assert "content" not in item["head"]["responses"]["200"]
    assert "content" not in item["head"]["responses"]["202"]

    get_variants = item["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    statuses = {
        variant["properties"].get("status", {}).get("enum", [None])[0]
        for variant in get_variants["properties"]["data"]["oneOf"]
    }
    assert statuses == {None, "not_generated"}

    post_variants = item["post"]["responses"]["202"]["content"]["application/json"]["schema"]
    assert {
        variant["properties"]["status"]["enum"][0] for variant in post_variants["properties"]["data"]["oneOf"]
    } == {"pending", "generating"}


def test_print_job_create_contract_excludes_storage_and_routing_capabilities():
    schema = build_schema(None)
    item = schema["paths"]["/api/v1/printing/jobs/"]
    assert _operation_methods(item) == {"get", "head", "post"}
    operation = item["post"]
    assert operation["security"] == [
        {"sessionAuth": []},
        {"cookieSession": [], "csrfHeader": []},
    ]
    assert (
        set(operation["responses"])
        == {
            "201",
            "400",
            "401",
            "403",
            "404",
            "409",
            "422",
            "429",
        }
        | TENANT_RUNTIME_RESPONSES
    )
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PrintJobCreateRequest"
    }

    request_schema = schema["components"]["schemas"]["PrintJobCreateRequest"]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["properties"]) == {
        "source",
        "source_id",
        "attachment_index",
        "pages",
        "copies",
        "color",
        "duplex",
    }
    assert {"payload_s3_key", "branch", "cohort"}.isdisjoint(request_schema["properties"])


def test_print_reconciliation_contract_is_scoped_closed_and_never_automatic():
    schema = build_schema(None)
    paths = schema["paths"]

    history = paths["/api/v1/printing/jobs/{pk}/reconciliations/"]
    assert _operation_methods(history) == {"get", "head"}
    assert history["get"]["security"] == [{"sessionAuth": []}, {"cookieSession": []}]
    assert "printing:read" in history["get"]["description"]
    assert "content" not in history["head"]["responses"]["200"]

    reconcile = paths["/api/v1/printing/jobs/{pk}/reconcile/"]
    assert _operation_methods(reconcile) == {"post"}
    operation = reconcile["post"]
    assert operation["security"] == [
        {"sessionAuth": []},
        {"cookieSession": [], "csrfHeader": []},
    ]
    assert "printing:write" in operation["description"]
    assert "never" in operation["description"].lower()
    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "pk",
        "Idempotency-Key",
    }
    request = schema["components"]["schemas"]["PrintJobReconcileRequest"]
    assert request["additionalProperties"] is False
    assert request["required"] == ["outcome", "evidence_reference"]
    assert request["properties"]["outcome"]["enum"] == [
        "confirmed_printed",
        "confirmed_not_printed",
        "abandoned_unknown",
    ]
    evidence = schema["components"]["schemas"]["PrintJobReconciliation"]
    assert {"lease_id", "idempotency_key", "idempotency_key_hash"}.isdisjoint(evidence["properties"])


def test_compensation_contract_is_separate_typed_and_idempotent():
    schema = build_schema(None)
    policy = schema["paths"]["/api/v1/teachers/{pk}/payout-policy/"]
    assert _operation_methods(policy) == {"get", "head", "post", "put"}
    assert "compensation:read" in policy["get"]["description"]
    assert "compensation:write" in policy["put"]["description"]
    assert policy["put"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PayoutPolicyRequest"
    }
    request_schema = schema["components"]["schemas"]["PayoutPolicyRequest"]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["flat_amount_uzs"]["type"] == "string"

    prepare = schema["paths"]["/api/v1/teachers/{pk}/prepare-salary/"]
    assert _operation_methods(prepare) == {"post"}
    operation = prepare["post"]
    assert "compensation:run" in operation["description"]
    assert {parameter["name"] for parameter in operation["parameters"]} == {"Idempotency-Key"}
    assert operation["parameters"][0]["required"] is True
    assert (
        set(operation["responses"])
        == {
            "201",
            "400",
            "401",
            "403",
            "404",
            "409",
            "422",
            "429",
        }
        | TENANT_RUNTIME_RESPONSES
    )


def test_wallet_mutations_publish_mandatory_retry_key_and_closed_dto():
    schema = build_schema(None)
    request_schema = schema["components"]["schemas"]["WalletAmountRequest"]
    assert request_schema["additionalProperties"] is False
    assert request_schema["required"] == ["amount"]
    assert set(request_schema["properties"]) == {"amount", "note"}

    for action in ("topup", "spend", "refund"):
        path = schema["paths"][f"/api/v1/cards/wallets/{{student_id}}/{action}/"]
        assert _operation_methods(path) == {"post"}
        operation = path["post"]
        assert operation["security"] == [
            {"sessionAuth": []},
            {"cookieSession": [], "csrfHeader": []},
        ]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert set(parameters) == {"student_id", "Idempotency-Key"}
        assert parameters["Idempotency-Key"]["required"] is True
        assert parameters["Idempotency-Key"]["schema"] == {
            "type": "string",
            "minLength": 16,
            "maxLength": 128,
        }
        assert operation["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/WalletAmountRequest"
        }
        assert {"201", "400", "401", "402", "403", "404", "409", "422", "429"}.issubset(
            operation["responses"]
        )


def test_runtime_gate_responses_and_warning_dtos_are_published_globally():
    schema = build_schema(None)
    paths = schema["paths"]
    schemas = schema["components"]["schemas"]

    # Authentication is subscription-paywall allowlisted, but an inactive tenant can still
    # reject it. Ordinary tenant operations can be rejected by both middleware gates.
    assert "402" not in paths["/api/v1/auth/role-login/"]["post"]["responses"]
    assert _response_component(paths["/api/v1/auth/role-login/"]["post"], "503") == (
        "TemporarilyUnavailableError"
    )
    ordinary = paths["/api/v1/tasks/"]["get"]
    assert _response_component(ordinary, "402") == "SubscriptionRequiredError"
    assert _response_component(ordinary, "503") == "TemporarilyUnavailableError"

    assert schemas["SubscriptionRequiredError"]["additionalProperties"] is False
    assert schemas["SubscriptionRequiredError"]["properties"]["code"]["enum"] == ["subscription_required"]
    warning = schemas["RuntimeWarning"]
    assert warning["additionalProperties"] is False
    assert warning["required"] == ["code", "message", "affected_sections"]
    assert warning["properties"]["code"]["enum"] == ["information_delayed"]

    # Middleware can add warnings to any successful tenant response, so closed explicit
    # wrappers must admit the same top-level DTO rather than becoming invalid at runtime.
    for response_name in ("UserBootstrapResponse", "PrintJobResponse", "TaskPageResponse"):
        assert schemas[response_name]["properties"]["warnings"] == {
            "type": "array",
            "items": {"$ref": "#/components/schemas/RuntimeWarning"},
            "description": "Present when an optional dependency is degraded.",
        }


def test_component_enrichment_does_not_mutate_domain_contract_registries():
    from apps.payroll.openapi_contracts import PAYROLL_SCHEMAS

    original = deepcopy(PAYROLL_SCHEMAS)
    _components()
    assert original == PAYROLL_SCHEMAS


def test_critical_contract_drift_fails_schema_validation_closed():
    operation = OperationContract(
        method="POST",
        summary="Deliberately drifted test contract",
        security=PUBLIC_SECURITY,
        responses={"200": json_response("ok")},
    )

    @openapi_contract(path="/api/v1/test/drift/", operations=(operation,))
    def runtime_get_only(request):
        if request.method == "GET":
            return None
        return None

    contract = get_openapi_contract(runtime_get_only)
    assert contract is not None
    with pytest.raises(OpenAPIContractError, match="runtime accepts"):
        _validate_view_contract(
            contract=contract,
            callback=runtime_get_only,
            path="/api/v1/test/drift/",
            name="drift",
        )
