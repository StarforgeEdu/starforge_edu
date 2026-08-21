"""D3-F-9 — FULL cross-tenant sweep over every Day-3 NOTIFICATIONS endpoint (TD-1).

Lane C already ships a basic `test_cross_tenant_day3.py` (feed/unread/prefs/
templates GET + isolation). This Lane-F file is the EXHAUSTIVE attacker sweep:
every endpoint x every accepted verb (incl. read/read-all/announcements and the
own-rows-only same-tenant case). A tenant-A JWT on tenant-B's host must 401
``tenant_mismatch`` at the auth gate, before any object lookup.

(Distinct filename to avoid clobbering Lane C's pre-existing file — shared/other-
lane files are off-limits.)
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db

NOTIFICATION_ENDPOINTS = [
    ("get", "/api/v1/notifications/"),
    ("get", "/api/v1/notifications/unread-count/"),
    ("post", "/api/v1/notifications/1/read/"),
    ("post", "/api/v1/notifications/read-all/"),
    ("get", "/api/v1/notifications/preferences/"),
    ("put", "/api/v1/notifications/preferences/"),
    ("get", "/api/v1/notifications/templates/"),
    ("post", "/api/v1/notifications/templates/"),
    ("get", "/api/v1/notifications/templates/1/"),
    ("post", "/api/v1/notifications/announcements/"),
]


@pytest.mark.parametrize(("method", "url"), NOTIFICATION_ENDPOINTS)
def test_notifications_cross_tenant_token_rejected(tenant_a, tenant_b, user_in, client_for, method, url):
    from apps.auth.services import issue_token

    user = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        access = issue_token(user)["access"]

    client_b = client_for(tenant_b)
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = getattr(client_b, method)(url, {}, format="json")

    assert resp.status_code == 401, (url, resp.status_code, resp.content)
    assert resp.json()["code"] == "authentication_failed"


def test_feed_is_own_rows_only_same_tenant(tenant_a, user_in, as_user):
    """Within one tenant, a director's feed shows only their OWN rows (the
    queryset is request.user, not role-scoped)."""
    from apps.notifications.models import Notification

    other = user_in(tenant_a, roles=["student"])
    with schema_context(tenant_a.schema_name):
        Notification.objects.create(
            user=other,
            event_type="payments.payment_completed",
            title="not yours",
            body="x",
            dedupe_key="sweep-notif-own",
        )

    director = user_in(tenant_a, roles=["director"])
    body = as_user(tenant_a, director).get("/api/v1/notifications/").json()
    assert "not yours" not in [r.get("title") for r in body["results"]]
