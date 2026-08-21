from __future__ import annotations

from apps.access.validation import permission_catalogue
from core.openapi import build_schema
from core.permissions import ROLE_PERMISSION_MATRIX, Role

HTTP_METHODS = {"get", "head", "post", "put", "patch", "delete"}
SAFE_SECURITY = [{"sessionAuth": []}, {"cookieSession": []}]
UNSAFE_SECURITY = [
    {"sessionAuth": []},
    {"cookieSession": [], "csrfHeader": []},
]


def _methods(item: dict) -> set[str]:
    return set(item).intersection(HTTP_METHODS)


def test_crm_routes_publish_their_exact_runtime_methods_and_security():
    paths = build_schema(None)["paths"]
    expected = {
        "/api/v1/crm/stages/": {"get", "head", "post"},
        "/api/v1/crm/stages/{pk}/": {"get", "head", "patch"},
        "/api/v1/crm/sources/": {"get", "head", "post"},
        "/api/v1/crm/campaigns/": {"get", "head", "post"},
        "/api/v1/crm/funnel/": {"get", "head"},
        "/api/v1/crm/duplicates/": {"get", "head"},
        "/api/v1/crm/duplicates/{pk}/dismiss/": {"post"},
        "/api/v1/crm/duplicates/{pk}/merge/": {"post"},
        "/api/v1/crm/follow-ups/": {"get", "head"},
        "/api/v1/crm/follow-ups/{pk}/complete/": {"post"},
        "/api/v1/crm/follow-ups/{pk}/cancel/": {"post"},
        "/api/v1/crm/leads/": {"get", "head", "post"},
        "/api/v1/crm/leads/{pk}/": {"get", "head"},
        "/api/v1/crm/leads/{pk}/owner/": {"post"},
        "/api/v1/crm/leads/{pk}/transition/": {"post"},
        "/api/v1/crm/leads/{pk}/stage-history/": {"get", "head"},
        "/api/v1/crm/leads/{pk}/touches/": {"get", "head", "post"},
        "/api/v1/crm/leads/{pk}/follow-ups/": {"get", "head", "post"},
        "/api/v1/crm/leads/{pk}/attributions/": {"get", "head", "post"},
        "/api/v1/crm/leads/{pk}/detect-duplicates/": {"post"},
    }
    assert {path: _methods(paths[path]) for path in expected} == expected
    for path, methods in expected.items():
        for method in methods:
            operation = paths[path][method]
            assert operation["security"] == (SAFE_SECURITY if method in {"get", "head"} else UNSAFE_SECURITY)


def test_crm_writes_publish_closed_dtos_real_replay_statuses_and_idempotency_header():
    paths = build_schema(None)["paths"]
    for path, item in paths.items():
        if not path.startswith("/api/v1/crm/"):
            continue
        for method in _methods(item).intersection({"post"}):
            operation = item[method]
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            assert schema["additionalProperties"] is False
            assert "400" in operation["responses"]
            parameter = next(value for value in operation["parameters"] if value["name"] == "Idempotency-Key")
            assert parameter["required"] is True
            success_statuses = set(operation["responses"]).intersection({"200", "201"})
            assert "200" in success_statuses
            if path in {
                "/api/v1/crm/stages/",
                "/api/v1/crm/sources/",
                "/api/v1/crm/campaigns/",
                "/api/v1/crm/leads/",
                "/api/v1/crm/leads/{pk}/touches/",
                "/api/v1/crm/leads/{pk}/follow-ups/",
                "/api/v1/crm/leads/{pk}/attributions/",
            }:
                assert "201" in success_statuses


def test_crm_funnel_schema_names_every_unit_window_and_metric_shape():
    operation = build_schema(None)["paths"]["/api/v1/crm/funnel/"]["get"]
    data = operation["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["data"]
    assert data["additionalProperties"] is False
    for name in ("window", "scope", "states", "definitions"):
        assert data["properties"][name]["additionalProperties"] is False
    assert data["properties"]["conversion_fraction"]["maximum"] == 1
    assert data["properties"]["loss_fraction"]["maximum"] == 1
    assert data["properties"]["stages"]["items"]["additionalProperties"] is False


def test_crm_permissions_are_delegable_but_catalogue_management_stays_director_only():
    assert {"crm:read", "crm:write"} <= ROLE_PERMISSION_MATRIX[Role.HEAD_OF_DEPT]
    assert {"crm:read", "crm:write"} <= ROLE_PERMISSION_MATRIX[Role.REGISTRAR]
    assert {"crm:read", "crm:write", "crm:manage"} <= permission_catalogue()
