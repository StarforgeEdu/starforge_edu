"""Database-free regressions for public app-availability response contracts."""

from __future__ import annotations

import json

from django.http import HttpRequest, JsonResponse

from core.availability import STATUS_DEGRADED, STATUS_UNAVAILABLE
from core.middleware import AppAvailabilityMiddleware


def test_degraded_dependency_returns_a_safe_structured_warning(monkeypatch):
    monkeypatch.setattr("core.middleware.current_schema", lambda: "tenant_a")
    monkeypatch.setattr(
        "core.availability.resolve_status",
        lambda app: (STATUS_DEGRADED, [f"private dependency details for {app}"]),
    )
    request = HttpRequest()
    request.path = "/api/v1/attendance/records/"

    outcome = AppAvailabilityMiddleware(lambda _request: JsonResponse({"success": True}))._resolve(request)

    assert outcome == [
        {
            "code": "information_delayed",
            "message": "Some information may be delayed.",
            "affected_sections": ["attendance"],
        }
    ]


def test_unavailable_dependency_does_not_expose_internal_topology(monkeypatch):
    monkeypatch.setattr("core.middleware.current_schema", lambda: "tenant_a")
    private_detail = "private-ledger dependency failed"
    monkeypatch.setattr(
        "core.availability.resolve_status",
        lambda _app: (STATUS_UNAVAILABLE, [private_detail]),
    )
    request = HttpRequest()
    request.path = "/api/v1/finance/invoices/"

    outcome = AppAvailabilityMiddleware(lambda _request: JsonResponse({"success": True}))._resolve(request)

    assert isinstance(outcome, JsonResponse)
    body = json.loads(outcome.content)
    assert outcome.status_code == 503
    assert body == {
        "success": False,
        "code": "service_unavailable",
        "message": "This capability is temporarily unavailable.",
    }
    assert private_detail not in outcome.content.decode()


def test_warning_injection_merges_existing_entries_and_deduplicates_by_identity():
    existing = {
        "code": "information_delayed",
        "message": "The finance total is still being prepared.",
        "affected_sections": ["finance"],
    }
    response = JsonResponse({"success": True, "data": {}, "warnings": [existing]})
    attendance = {
        "code": "information_delayed",
        "message": "Some information may be delayed.",
        "affected_sections": ["attendance"],
    }

    AppAvailabilityMiddleware._inject_warnings(response, [attendance, dict(attendance)])

    assert json.loads(response.content)["warnings"] == [existing, attendance]
    assert response["Content-Length"] == str(len(response.content))


def test_warning_injection_never_changes_error_or_malformed_success_envelopes():
    warning = {
        "code": "information_delayed",
        "message": "Some information may be delayed.",
        "affected_sections": ["tasks"],
    }
    error = JsonResponse(
        {"success": False, "code": "conflict", "message": "Try again."},
        status=409,
    )
    malformed = JsonResponse({"success": True, "warnings": "legacy text"})

    AppAvailabilityMiddleware._inject_warnings(error, [warning])
    AppAvailabilityMiddleware._inject_warnings(malformed, [warning])

    assert "warnings" not in json.loads(error.content)
    assert json.loads(malformed.content)["warnings"] == "legacy text"
