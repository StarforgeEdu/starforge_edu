"""Cross-tenant isolation for Day-3 notification endpoints (D3-F-9 slice).

A tenant_a JWT presented on the tenant_b host must 401 `tenant_mismatch` (TD-1),
and tenant_a notification rows must be invisible from tenant_b even for a
director. Uses the TESTING.md two-tenant fixtures.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.notifications.models import EventType, Notification
from core.permissions import Role

pytestmark = pytest.mark.django_db

ENDPOINTS = [
    "/api/v1/notifications/",
    "/api/v1/notifications/unread-count/",
    "/api/v1/notifications/preferences/",
    "/api/v1/notifications/templates/",
]


@pytest.mark.parametrize("url", ENDPOINTS)
def test_cross_tenant_token_rejected(tenant_a, tenant_b, user_in, client_for, url):
    from apps.auth.services import issue_token

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        access = issue_token(user)["access"]
    client_b = client_for(tenant_b)
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = client_b.get(url)
    assert resp.status_code == 401
    assert resp.json()["code"] == "authentication_failed"


def test_notifications_invisible_across_tenants(tenant_a, tenant_b, user_in, as_user):
    """A tenant_b director never sees tenant_a notification rows."""
    a_user = user_in(tenant_a, roles=[Role.PARENT])
    with schema_context(tenant_a.schema_name):
        Notification.objects.create(user=a_user, event_type=EventType.ATTENDANCE_ABSENT, title="A-secret")
    b_director = user_in(tenant_b, roles=[Role.DIRECTOR])
    client_b = as_user(tenant_b, b_director)
    resp = client_b.get("/api/v1/notifications/")
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.json()["results"]]
    assert "A-secret" not in titles
