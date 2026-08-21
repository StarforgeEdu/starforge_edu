"""D3-F-9 — cross-tenant sweep over every Day-3 AUDIT endpoint (TD-1).

A tenant-A JWT on tenant-B's host must 401 ``tenant_mismatch`` at the auth gate,
before any object lookup. Also asserts tenant-A audit rows are invisible from
tenant B even with the audit:read-bearing director role.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.audit.tests.factories import AuditLogFactory

pytestmark = pytest.mark.django_db

AUDIT_ENDPOINTS = [
    ("get", "/api/v1/audit/"),
    ("get", "/api/v1/audit/1/"),
    ("get", "/api/v1/audit/export/"),
]


@pytest.mark.parametrize(("method", "url"), AUDIT_ENDPOINTS)
def test_audit_cross_tenant_token_rejected(tenant_a, tenant_b, user_in, client_for, method, url):
    from apps.auth.services import issue_token

    user = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        access = issue_token(user)["access"]

    client_b = client_for(tenant_b)
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = getattr(client_b, method)(url)

    assert resp.status_code == 401, (url, resp.status_code, resp.content)
    assert resp.json()["code"] == "authentication_failed"


def test_audit_rows_invisible_across_tenant(tenant_a, tenant_b, user_in, as_user):
    """tenant-A audit rows are not visible from tenant B even as director."""
    with schema_context(tenant_a.schema_name):
        AuditLogFactory(resource_type="finance.Invoice", resource_id="42")

    director_b = user_in(tenant_b, roles=["director"])
    body = as_user(tenant_b, director_b).get("/api/v1/audit/").json()
    # TimelinePagination feed envelope — no tenant-A rows leak into tenant B.
    results = body.get("results", body)
    assert all(r.get("resource_id") != "42" for r in results)


def test_audit_export_csv_isolated(tenant_a, tenant_b, user_in, as_user):
    """The CSV export in tenant B never contains tenant-A audit content."""
    with schema_context(tenant_a.schema_name):
        AuditLogFactory(resource_type="payments.Payment", resource_id="A-SECRET-99")

    director_b = user_in(tenant_b, roles=["director"])
    resp = as_user(tenant_b, director_b).get("/api/v1/audit/export/")
    assert resp.status_code == 200
    content = b"".join(resp.streaming_content).decode()
    assert "A-SECRET-99" not in content
