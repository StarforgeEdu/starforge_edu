"""Blanket /api/ rate limit (core.middleware.ApiRateLimitMiddleware).

The migrated plain FBVs bypass DRF dispatch, so DRF's UserRateThrottle /
AnonRateThrottle no longer cover them — this middleware restores the blanket
caps for BOTH view styles. Buckets: per Bearer token (hashed) at the user rate,
per client IP at the anon rate. The autouse ``_clear_cache`` fixture resets the
buckets around every test.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from core.middleware import _parse_rate
from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/students/"  # a migrated (plain-view) endpoint


def test_parse_rate_formats():
    assert _parse_rate("1000/min") == (1000, 60)
    assert _parse_rate("60/minute") == (60, 60)
    assert _parse_rate("5/sec") == (5, 1)
    assert _parse_rate("100/hour") == (100, 3600)
    assert _parse_rate("2/day") == (2, 86400)


@override_settings(API_RATELIMIT_ANON="3/min")
def test_anon_flood_is_throttled_with_envelope(tenant_a, client_for):
    client = client_for(tenant_a)
    for _ in range(3):
        assert client.get(URL).status_code == 401  # unauthenticated but under the cap
    resp = client.get(URL)
    assert resp.status_code == 429
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "throttled"
    assert resp["Retry-After"]


@override_settings(API_RATELIMIT_USER="2/min")
def test_authenticated_flood_is_throttled_per_token(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)
    assert client.get(URL).status_code == 200
    assert client.get(URL).status_code == 200
    assert client.get(URL).status_code == 429  # third request over the 2/min cap

    # A DIFFERENT user's token is a separate bucket — not collateral damage.
    other, _ = as_role(Role.TEACHER)
    assert other.get(URL).status_code == 200


@override_settings(API_RATELIMIT_ANON="1/min")
def test_non_api_paths_are_not_limited(tenant_a, client_for):
    client = client_for(tenant_a)
    assert client.get(URL).status_code == 401  # consumes the single anon slot
    # /healthz is outside /api/ — never throttled (ops probes must always answer).
    for _ in range(3):
        assert client.get("/healthz/live").status_code == 200


@override_settings(API_RATELIMIT_ANON="1/min")
def test_payment_webhooks_are_exempt_from_the_blanket_limiter(tenant_a, client_for):
    """R4/CONF4: provider webhooks (/api/v1/webhooks/...) are signature-authed and
    pushed from the provider's fixed IP; the anon limiter (keyed on IP, before tenant
    resolution) would collapse ALL tenants' callbacks into one bucket and 429 a
    payment callback during a burst — breaking the money path. They must be exempt."""
    client = client_for(tenant_a)
    # Hammer a webhook path well past the 1/min anon cap; it must never 429 from the
    # blanket limiter (an unknown center slug 404s, a bad signature is handled in-view —
    # neither is a 429).
    for _ in range(4):
        resp = client.post("/api/v1/webhooks/click/nonexistent-center/", {}, format="json")
        assert resp.status_code != 429, resp.content


@override_settings(ADMIN_LOGIN_RATELIMIT="3/min")
def test_admin_login_bruteforce_is_throttled(tenant_a, client_for):
    """R1-07: /admin/login/ is not under /api/, so it bypassed the blanket limiter,
    leaving staff/superuser credentials open to unlimited brute-force. The dedicated
    admin-login throttle must cap the POST (429 with the standard envelope)."""
    client = client_for(tenant_a)
    for _ in range(3):
        resp = client.post("/admin/login/", {"username": "root", "password": "x"})
        assert resp.status_code != 429  # under the cap (auth fails, but not throttled yet)
    throttled = client.post("/admin/login/", {"username": "root", "password": "x"})
    assert throttled.status_code == 429
    assert throttled.json()["code"] == "throttled"
    assert throttled["Retry-After"]


@override_settings(API_RATELIMIT_ANON="1/min")
def test_options_preflight_is_exempt(tenant_a, client_for):
    client = client_for(tenant_a)
    assert client.get(URL).status_code == 401  # consumes the single anon slot
    # CORS preflights must never be throttled (DRF's view-level throttles never saw
    # them either) — they'd otherwise starve a browser SPA of its real requests.
    assert client.options(URL).status_code != 429
