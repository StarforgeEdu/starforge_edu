"""Database-free regressions for rate-limit configuration and early failures."""

from __future__ import annotations

import json

import pytest
from django.http import JsonResponse
from django.test import RequestFactory, override_settings

from core.exceptions import ServiceUnavailableException
from core.middleware import ApiRateLimitMiddleware, _parse_rate
from core.rate_config import RateConfigurationError


def _must_not_run(_request):
    raise AssertionError("an unavailable rate limiter must fail before the view")


def test_rate_parser_accepts_only_positive_known_fixed_windows():
    assert _parse_rate("1/sec") == (1, 1)
    assert _parse_rate(" 60 / minutes ") == (60, 60)
    assert _parse_rate("100/HOURS") == (100, 3600)
    assert _parse_rate("2/days") == (2, 86400)


@pytest.mark.parametrize(
    "value",
    [
        None,
        60,
        "",
        "0/min",
        "-1/min",
        "+1/min",
        "1.5/min",
        "1",
        "1//min",
        "1/week",
        "2147483648/min",
    ],
)
def test_rate_parser_rejects_values_that_cannot_be_enforced_safely(value):
    with pytest.raises(RateConfigurationError):
        _parse_rate(value, setting_name="TEST_RATE")


@override_settings(API_RATELIMIT_PREAUTH="0/min")
def test_invalid_api_rate_setting_returns_stable_temporary_unavailability():
    request = RequestFactory().get(
        "/api/v1/students/",
        HTTP_AUTHORIZATION="Bearer presented-but-not-yet-validated",
    )

    response = ApiRateLimitMiddleware(_must_not_run)(request)

    assert response.status_code == 503
    assert json.loads(response.content) == {
        "success": False,
        "code": "temporarily_unavailable",
        "message": "This operation is temporarily unavailable.",
    }


def test_rate_limit_cache_outage_returns_stable_temporary_unavailability(monkeypatch):
    def unavailable(**_kwargs):
        raise ServiceUnavailableException(
            "This operation is temporarily unavailable.",
            code="temporarily_unavailable",
        )

    monkeypatch.setattr("core.ratelimit.check_rate", unavailable)
    request = RequestFactory().get(
        "/api/v1/students/",
        HTTP_AUTHORIZATION="Bearer presented-but-not-yet-validated",
    )

    response = ApiRateLimitMiddleware(_must_not_run)(request)

    assert response.status_code == 503
    assert response["Content-Type"].startswith("application/json")
    assert json.loads(response.content) == {
        "success": False,
        "code": "temporarily_unavailable",
        "message": "This operation is temporarily unavailable.",
    }


@override_settings(API_RATELIMIT_PREAUTH="10/min", API_RATELIMIT_ANON="10/min")
def test_only_exact_agent_wire_shape_skips_anonymous_bucket(monkeypatch):
    calls: list[str] = []

    def record(*, scope, **_kwargs):
        calls.append(scope)

    monkeypatch.setattr("core.ratelimit.check_rate", record)
    middleware = ApiRateLimitMiddleware(lambda _request: JsonResponse({"ok": True}))

    valid = RequestFactory().post(
        "/api/v1/printing/agent/claim/",
        HTTP_AUTHORIZATION=f"Agent {'a' * 64}",
    )
    assert middleware(valid).status_code == 200
    assert calls == ["api_pre_auth"]

    calls.clear()
    malformed = RequestFactory().post(
        "/api/v1/printing/agent/claim/",
        HTTP_AUTHORIZATION=f"Agent {'a' * 63}",
    )
    assert middleware(malformed).status_code == 200
    assert calls == ["api_pre_auth", "api_anon"]


@override_settings(API_RATELIMIT_PREAUTH="10/min", API_RATELIMIT_ANON="10/min")
def test_only_registered_payment_callback_shapes_bypass_blanket_limit(monkeypatch):
    calls: list[str] = []

    def record(*, scope, **_kwargs):
        calls.append(scope)

    monkeypatch.setattr("core.ratelimit.check_rate", record)
    middleware = ApiRateLimitMiddleware(lambda _request: JsonResponse({"ok": True}))

    callback = RequestFactory().post("/api/v1/webhooks/click/center-one/")
    assert middleware(callback).status_code == 200
    assert calls == []

    for request in (
        RequestFactory().post("/api/v1/webhooks/unknown/center-one/"),
        RequestFactory().post("/api/v1/webhooks/click/center-one/extra/"),
        RequestFactory().get("/api/v1/webhooks/click/center-one/"),
    ):
        calls.clear()
        assert middleware(request).status_code == 200
        assert calls == ["api_pre_auth", "api_anon"]
