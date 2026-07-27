"""D3-F-6 — paywall behavior (SubscriptionGateMiddleware, D3-E-4).

A `suspended` subscription must paywall the tenant's domain API with 402
`subscription_required`, while the allowlist (auth, /admin/, /healthz,
/api/schema) and the public-schema webhook intake stay reachable, and a
different tenant is unaffected.

AUTH PIVOT (DAY-3.md is stale): the reachable auth endpoint is
`POST /api/v1/auth/login/` (and `/api/v1/auth/password/reset/request/`) — there
is NO `/auth/otp/*`. The middleware allowlists the whole `/api/v1/auth/` prefix.

Subscription/Plan are PUBLIC-schema rows: create them WITHOUT a schema_context
(the autouse `_reset_schema_to_public` fixture leaves us on public). The status
selector caches for 60s but the autouse `_clear_cache` fixture resets it per
test, so each test reads the status it just wrote.
"""

from __future__ import annotations

import pytest

from apps.billing.tests.factories import PlanFactory, SubscriptionFactory

pytestmark = pytest.mark.django_db

STUDENTS_URL = "/api/v1/students/"


def _set_status(center, status):
    """Create/replace the Center's subscription with the given status (public)."""
    from apps.billing.models import Subscription

    Subscription.objects.filter(center=center).delete()
    return SubscriptionFactory(center=center, plan=PlanFactory(code="paywall-plan"), status=status)


# --------------------------------------------------------------------------- #
# Suspended tenant: domain API paywalled
# --------------------------------------------------------------------------- #
def test_suspended_tenant_students_402(tenant_a, user_in, as_user):
    director = user_in(tenant_a, roles=["director"])
    _set_status(tenant_a, "suspended")
    resp = as_user(tenant_a, director).get(STUDENTS_URL)
    assert resp.status_code == 402
    assert resp.json()["code"] == "subscription_required"


def test_active_tenant_students_not_paywalled(tenant_a, user_in, as_user):
    director = user_in(tenant_a, roles=["director"])
    _set_status(tenant_a, "active")
    resp = as_user(tenant_a, director).get(STUDENTS_URL)
    assert resp.status_code == 200


def test_trialing_tenant_not_paywalled(tenant_a, user_in, as_user):
    director = user_in(tenant_a, roles=["director"])
    _set_status(tenant_a, "trialing")
    resp = as_user(tenant_a, director).get(STUDENTS_URL)
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Allowlist stays reachable even when suspended
# --------------------------------------------------------------------------- #
def test_suspended_tenant_login_reachable(tenant_a, client_for):
    """AUTH PIVOT: /api/v1/auth/login/ is allowlisted. A login attempt must NOT
    402 — it reaches the view (401 invalid_credentials for bad creds is fine;
    the point is it is NOT 402)."""
    _set_status(tenant_a, "suspended")
    resp = client_for(tenant_a).post(
        "/api/v1/auth/login/",
        {"username": "nobody", "password": "wrong"},
        format="json",
    )
    assert resp.status_code != 402
    assert resp.status_code in (400, 401)


def test_suspended_tenant_password_reset_reachable(tenant_a, client_for):
    """The other reachable auth endpoint (OTP repurposed to password reset)."""
    _set_status(tenant_a, "suspended")
    resp = client_for(tenant_a).post(
        "/api/v1/auth/password/reset/request/",
        {"identifier": "+998901234567"},
        format="json",
    )
    # anti-enumeration: always 202; never paywalled.
    assert resp.status_code != 402
    assert resp.status_code in (200, 202)


def test_suspended_tenant_healthz_reachable(tenant_a, client_for):
    _set_status(tenant_a, "suspended")
    resp = client_for(tenant_a).get("/healthz/live")
    assert resp.status_code != 402
    assert resp.status_code == 200


def test_suspended_tenant_schema_reachable(tenant_a, client_for):
    _set_status(tenant_a, "suspended")
    resp = client_for(tenant_a).get("/api/schema/")
    assert resp.status_code != 402


# --------------------------------------------------------------------------- #
# Isolation: other tenant unaffected
# --------------------------------------------------------------------------- #
def test_other_tenant_unaffected_when_a_suspended(tenant_a, tenant_b, user_in, as_user):
    _set_status(tenant_a, "suspended")
    _set_status(tenant_b, "active")
    director_b = user_in(tenant_b, roles=["director"])
    resp = as_user(tenant_b, director_b).get(STUDENTS_URL)
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Public schema no-op: webhook intake still works for a suspended tenant
# --------------------------------------------------------------------------- #
def test_suspended_tenant_public_webhook_still_works(tenant_a, public_tenant):
    """The middleware is a no-op on the public schema, so a suspended center's
    webhook intake (TD-6, public-schema URL) is NOT paywalled.

    The webhook is posted to the apex/``testserver`` host: ``public_tenant`` maps
    that host to the public schema (else the POST 404s before reaching the gate)."""
    from apps.payments.tests import _helpers as helpers
    from apps.payments.tests import builders

    _set_status(tenant_a, "suspended")
    helpers.seed_provider_configs(tenant_a)
    helpers.seed_open_invoice(tenant_a, number="INV-2026-000001", amount_uzs="150000.00")

    resp = helpers.public_client().post(
        helpers.webhook_url("payme", tenant_a.schema_name),
        data=builders.payme_check_perform(amount_tiyin=15_000_000, account={"order_id": "INV-2026-000001"}),
        format="json",
        **builders.payme_auth_headers(),
    )
    # Reaches the webhook handler (HTTP 200 JSON-RPC), never the 402 paywall.
    assert resp.status_code == 200
    assert resp.status_code != 402


def test_no_subscription_row_passes_through(tenant_a, user_in, as_user):
    """A Center with no subscription row yet (selector returns None) is NOT
    paywalled — provisioning auto-creates a trialing one, but the gate must not
    fail-closed on a missing row."""
    from apps.billing.models import Subscription

    Subscription.objects.filter(center=tenant_a).delete()
    director = user_in(tenant_a, roles=["director"])
    resp = as_user(tenant_a, director).get(STUDENTS_URL)
    assert resp.status_code != 402
