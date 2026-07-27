"""The custom OpenAPI schema (core.openapi) covers the whole off-DRF API and serves via
/api/schema/ (+ Swagger UI / Redoc). Before this, drf-spectacular only saw the lone DRF
'reports' app, so a client dev could not discover ~320 endpoints or generate an SDK."""

from __future__ import annotations

import pytest

from core.openapi import build_schema

pytestmark = pytest.mark.django_db


def test_schema_covers_the_layered_api():
    s = build_schema(None)  # None -> ROOT_URLCONF (the tenant API)
    assert s["openapi"] == "3.0.3"
    paths = s["paths"]
    # Far more than the ~5 reports endpoints drf-spectacular alone produced.
    assert len(paths) > 200
    for p in (
        "/api/v1/students/",
        "/api/v1/students/{pk}/",
        "/api/v1/cohorts/",
        "/api/v1/finance/invoices/",
        "/api/v1/approvals/requests/",
        "/api/v1/auth/login/",
    ):
        assert p in paths, p
    # Methods are introspected accurately from the views.
    assert set(m for m in paths["/api/v1/students/"] if m in ("get", "post")) == {"get", "post"}
    assert {"get", "patch", "put", "delete"} <= set(paths["/api/v1/students/{pk}/"])


def test_reports_drf_app_is_covered():
    """The lone DRF app (reports) uses a DefaultRouter -> RegexPattern routes + viewset action
    maps. The generator must translate those (not collapse every route onto /api/v1/reports/,
    nor default to GET-only). Regression for the two schema-correctness bugs."""
    s = build_schema(None)
    paths = s["paths"]
    assert "/api/v1/reports/{pk}/" in paths  # library detail (was collapsed away)
    assert {"get", "post"} <= set(paths["/api/v1/reports/runs/"])  # list + create
    assert "get" in paths["/api/v1/reports/runs/{pk}/"]  # retrieve (pk was lost)
    assert {"get", "post"} <= set(paths["/api/v1/reports/schedules/"])
    sched = set(m for m in paths["/api/v1/reports/schedules/{pk}/"] if m in ("get", "put", "patch"))
    assert "patch" in sched
    assert "put" not in sched  # ReportScheduleViewSet.http_method_names drops PUT
    assert not any("{format}" in p for p in paths)  # DRF .json/.api suffix routes skipped


def test_auth_and_security_scheme():
    s = build_schema(None)
    login = s["paths"]["/api/v1/auth/login/"]["post"]  # POST (via @require_POST), public
    assert "security" not in login
    students = s["paths"]["/api/v1/students/"]["get"]  # secured
    assert students.get("security") == [{"sessionAuth": []}]
    comps = s["components"]
    assert comps["securitySchemes"]["sessionAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "description": comps["securitySchemes"]["sessionAuth"]["description"],
    }
    assert {"Success", "Error", "Pagination"} <= set(comps["schemas"])


def test_path_params_are_typed():
    s = build_schema(None)
    params = s["paths"]["/api/v1/students/{pk}/"].get("parameters", [])
    pk = next((p for p in params if p["name"] == "pk"), None)
    assert pk is not None
    assert pk["in"] == "path"
    assert pk["required"] is True
    assert pk["schema"]["type"] == "integer"


def test_schema_endpoint_served_as_json(tenant_a, client_for):
    resp = client_for(tenant_a).get("/api/schema/")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/json")
    body = resp.json()
    assert body["openapi"] == "3.0.3"
    assert "/api/v1/students/" in body["paths"]
    assert body["servers"][0]["url"].startswith("http")


def test_swagger_ui_and_redoc_render(tenant_a, client_for):
    swagger = client_for(tenant_a).get("/api/schema/swagger-ui/")
    assert swagger.status_code == 200
    assert b"swagger" in swagger.content.lower()
    redoc = client_for(tenant_a).get("/api/schema/redoc/")
    assert redoc.status_code == 200
