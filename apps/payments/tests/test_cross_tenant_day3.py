"""D3-F-9 — cross-tenant sweep over every Day-3 PAYMENTS endpoint (TD-1).

A tenant-A JWT on tenant-B's host must 401 ``tenant_mismatch`` at the auth gate,
before any object lookup. NOTE: the public-schema WEBHOOK routes
(``/api/v1/webhooks/...``) are deliberately EXCLUDED — they have
``authentication_classes = []`` (provider signature is the auth, not JWT) and
their cross-tenant defense is the per-tenant ProviderConfig signature check
(covered in test_webhook_attacks.py D3-F-3), not the JWT tenant claim.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db

# Tenant-side payments endpoints (JWT-authed). Webhooks are public — see docstring.
PAYMENT_ENDPOINTS = [
    ("get", "/api/v1/payments/"),
    ("get", "/api/v1/payments/1/"),
    ("post", "/api/v1/payments/checkout/"),
    ("post", "/api/v1/payments/1/allocate/"),
    ("post", "/api/v1/payments/1/refund/"),
    ("get", "/api/v1/payments/reconciliation/?date=2026-06-16"),
    ("get", "/api/v1/payments/1/receipt/"),
    ("get", "/api/v1/payments/provider-configs/"),
    ("post", "/api/v1/payments/provider-configs/"),
    ("get", "/api/v1/payments/provider-configs/1/"),
]


@pytest.mark.parametrize(("method", "url"), PAYMENT_ENDPOINTS)
def test_payments_cross_tenant_token_rejected(tenant_a, tenant_b, user_in, client_for, method, url):
    from apps.auth.services import issue_token

    user = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        access = issue_token(user)["access"]

    client_b = client_for(tenant_b)
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = getattr(client_b, method)(url, {}, format="json")

    assert resp.status_code == 401, (url, resp.status_code, resp.content)
    assert resp.json()["code"] == "authentication_failed"


def test_payments_list_invisible_across_tenant(tenant_a, tenant_b, user_in, as_user):
    """tenant-A payments are not visible from tenant B even with director role."""
    from decimal import Decimal

    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        Payment.objects.create(
            provider="cash",
            amount_uzs=Decimal("150000.00"),
            status="completed",
            idempotency_key="xt-pay-1",
            account_ref="INV-2026-000001",
        )

    director_b = user_in(tenant_b, roles=["director"])
    body = as_user(tenant_b, director_b).get("/api/v1/payments/").json()
    assert body["pagination"]["total"] == 0


def test_provider_config_credentials_never_echoed(tenant_a, user_in, as_user):
    """Defense-in-depth: ProviderConfig credential fields are write-only and must
    not appear in the serialized list/detail response (TD-11)."""
    from apps.payments.tests import _helpers as helpers

    helpers.seed_provider_configs(tenant_a)
    director = as_user(tenant_a, user_in(tenant_a, roles=["director"]))
    resp = director.get("/api/v1/payments/provider-configs/")
    assert resp.status_code == 200
    text = resp.content.decode()
    for secret in ("click_test_secret_key", "payme_test_secret_key", "uzum_test_secret_key"):
        assert secret not in text, f"credential leaked in provider-config response: {secret}"
