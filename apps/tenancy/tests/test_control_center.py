"""Lane E — platform control center (D4-LE-1..7).

All tests run on the PUBLIC schema surface (`testserver` host → public via the
`public_tenant` fixture). Platform staff are public-schema users (TD-3); a
tenant-schema user must be rejected.

The impersonation WRITE-deny (403 read_only_token) and EXPIRED-token (401)
behaviours depend on the core wiring returned in integration_needed
(core/authentication.py surfacing `read_only`, core/permissions.py
`DenyWriteForReadOnlyToken`, core/viewsets.py adding it). Those tests run the
real flow when the wiring is present and skip with a clear reason until the
orchestrator applies it — never a false green.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context
from rest_framework.test import APIClient

from apps.billing.models import Plan, Subscription

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
PASSWORD = "s3cret-Platform!"


@pytest.fixture
def platform_admin(public_tenant):
    from apps.users.models import User

    return User.objects.create_superuser(username="ctl-padmin", password=PASSWORD)


@pytest.fixture
def staff_admin(public_tenant):
    """is_staff (not superuser) — IsAdminUser passes; superuser bypass excluded."""
    from apps.users.models import User

    user = User.objects.create_user(username="ctl-staff", password=PASSWORD)
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    return user


@pytest.fixture
def staff_client(staff_admin):
    # The layered control-center views authenticate with the custom session
    # authenticator (not DRF force_authenticate) — mint a real public-schema
    # session key so a Bearer credential exercises the platform-admin gate.
    from core.session_auth import create_session

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {create_session(staff_admin).key}")
    return client


def test_platform_admin_can_obtain_api_session_from_public_login(platform_admin, tenant_a):
    client = APIClient()

    login = client.post(
        "/api/v1/auth/login/",
        {"username": platform_admin.username, "password": PASSWORD},
        format="json",
    )

    assert login.status_code == 200, login.content
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['data']['access']}")
    assert client.get("/api/v1/platform/centers/").status_code == 200
    assert client.post(
        "/api/v1/auth/role-login/",
        {"username": platform_admin.username, "password": PASSWORD},
        format="json",
    ).status_code == 404


def _ensure_plan(max_students=1000):
    return Plan.objects.get_or_create(
        code="starter",
        defaults={
            "name": "Starter",
            "max_students": max_students,
            "max_branches": 5,
            "ai_tokens_month": 1_000_000,
            "storage_gb": 10,
            "price_uzs": "100000.00",
        },
    )[0]


def _set_subscription(center, *, status=Subscription.Status.ACTIVE, period_end=None):
    plan = _ensure_plan()
    now = timezone.now()
    sub, _ = Subscription.objects.update_or_create(
        center=center,
        defaults={
            "plan": plan,
            "status": status,
            "current_period_start": now,
            "current_period_end": period_end or (now + timedelta(days=30)),
        },
    )
    return sub


# ---------------------------------------------------------------------------
# D4-LE-1 — lifecycle: suspend → 402 → activate → 200 (Day-3 paywall reuse)
# ---------------------------------------------------------------------------
def test_suspend_then_activate_round_trip(
    staff_client, tenant_a, client_for, django_capture_on_commit_callbacks
):
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)

    # Before: a gated tenant endpoint is reachable (200/401 — not a paywall 402).
    pre = client_for(tenant_a).get("/api/v1/org/settings/")
    assert pre.status_code != 402

    # Subscription-cache invalidation runs on_commit; execute it so the paywall
    # sees the new status within this test transaction.
    with django_capture_on_commit_callbacks(execute=True):
        resp = staff_client.post(f"/api/v1/platform/centers/{tenant_a.pk}/suspend/", {}, format="json")
    assert resp.status_code == 200
    # Suspension is SOFT: is_active stays True (the 402 paywall gates the API; a
    # 503 InactiveTenant would otherwise shadow the paywall and block auth too).
    assert resp.json()["data"]["is_active"] is True
    assert Subscription.objects.get(center=tenant_a).status == "suspended"

    # Tenant API now blocked by the Day-3 paywall (402 subscription_required).
    blocked = client_for(tenant_a).get("/api/v1/org/settings/")
    assert blocked.status_code == 402
    assert blocked.json()["code"] == "subscription_required"

    with django_capture_on_commit_callbacks(execute=True):
        resp = staff_client.post(f"/api/v1/platform/centers/{tenant_a.pk}/activate/", {}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["is_active"] is True
    assert Subscription.objects.get(center=tenant_a).status == "active"

    # Reactivated → tenant API returns past the paywall again.
    after = client_for(tenant_a).get("/api/v1/org/settings/")
    assert after.status_code != 402


def test_extend_trial_moves_trial_ends_at(staff_client, tenant_a):
    tenant_a.trial_ends_at = None
    tenant_a.save(update_fields=["trial_ends_at"])
    resp = staff_client.post(
        f"/api/v1/platform/centers/{tenant_a.pk}/extend-trial/", {"days": 14}, format="json"
    )
    assert resp.status_code == 200
    tenant_a.refresh_from_db()
    assert tenant_a.trial_ends_at is not None
    assert tenant_a.trial_ends_at > timezone.now() + timedelta(days=13)


def test_extend_trial_rejects_zero_days(staff_client, tenant_a):
    resp = staff_client.post(
        f"/api/v1/platform/centers/{tenant_a.pk}/extend-trial/", {"days": 0}, format="json"
    )
    assert resp.status_code == 400


def test_create_center_delegates_to_provision(staff_client):
    resp = staff_client.post(
        "/api/v1/platform/centers/",
        {"name": "Made Via API", "slug": "viaapi", "primary_domain": "viaapi.localhost"},
        format="json",
    )
    assert resp.status_code == 201
    from apps.tenancy.models import Center

    center = Center.objects.get(slug="viaapi")
    assert center.schema_name == "viaapi"
    # Provisioning seeds CenterSettings (TD-13).
    with schema_context("viaapi"):
        from apps.org.models import CenterSettings

        assert CenterSettings.objects.filter(pk=1).exists()


def test_create_center_trims_padded_contact_email(staff_client):
    """A padded contact_email must be trimmed+accepted (DRF EmailField parity),
    not 400'd — and stored without the surrounding whitespace."""
    resp = staff_client.post(
        "/api/v1/platform/centers/",
        {
            "name": "Padded Email",
            "slug": "padmail",
            "primary_domain": "padmail.localhost",
            "contact_email": "  ops@padmail.example  ",
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["data"]["contact_email"] == "ops@padmail.example"
    from apps.tenancy.models import Center

    assert Center.objects.get(slug="padmail").contact_email == "ops@padmail.example"


def test_non_staff_public_user_403(public_tenant, tenant_a):
    """A public-schema NON-staff user is forbidden on the control center."""
    from apps.users.models import User
    from core.session_auth import create_session

    user = User.objects.create_user(username="ctl-plain", password=PASSWORD)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {create_session(user).key}")
    assert client.post(f"/api/v1/platform/centers/{tenant_a.pk}/suspend/", {}).status_code == 403


# ---------------------------------------------------------------------------
# D4-LE-2 — usage endpoint (snapshots + live DAU), two-tenant isolation
# ---------------------------------------------------------------------------
def test_usage_endpoint_series_and_live_today(staff_client, tenant_a):
    from apps.billing.models import UsageSnapshot

    today = timezone.localdate()
    UsageSnapshot.objects.create(
        center=tenant_a, date=today, students_count=42, storage_bytes=999, ai_tokens_used=123
    )
    # One active user "seen" today → live DAU == 1.
    with schema_context(tenant_a.schema_name):
        from apps.users.tests.factories import UserFactory

        UserFactory(last_seen_at=timezone.now())

    resp = staff_client.get(f"/api/v1/platform/centers/{tenant_a.pk}/usage/?days=30")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert any(p["students"] == 42 for p in body["series"])
    assert body["today"]["students"] == 42  # carried from latest snapshot
    assert body["today"]["dau"] >= 1


def test_usage_endpoint_two_tenant_isolation(staff_client, tenant_a, tenant_b):
    from apps.billing.models import UsageSnapshot

    today = timezone.localdate()
    UsageSnapshot.objects.create(center=tenant_a, date=today, students_count=11)
    UsageSnapshot.objects.create(center=tenant_b, date=today, students_count=22)

    a = staff_client.get(f"/api/v1/platform/centers/{tenant_a.pk}/usage/").json()["data"]
    b = staff_client.get(f"/api/v1/platform/centers/{tenant_b.pk}/usage/").json()["data"]
    assert all(p["students"] in (11, 0) for p in a["series"])
    assert all(p["students"] in (22, 0) for p in b["series"])


def test_usage_invalid_days_400(staff_client, tenant_a):
    resp = staff_client.get(f"/api/v1/platform/centers/{tenant_a.pk}/usage/?days=abc")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# D4-LE-3 — subscription management (flat /platform/subscriptions/)
# ---------------------------------------------------------------------------
def test_subscription_list_and_patch_reactivates(
    staff_client, tenant_a, client_for, django_capture_on_commit_callbacks
):
    sub = _set_subscription(tenant_a, status=Subscription.Status.SUSPENDED)
    tenant_a.is_active = True
    tenant_a.save(update_fields=["is_active"])

    listed = staff_client.get("/api/v1/platform/subscriptions/")
    assert listed.status_code == 200
    rows = listed.json()["data"]  # billing is now layered ({success, data, pagination})
    assert sub.pk in {r["id"] for r in rows}

    # Suspended → paywall 402.
    assert client_for(tenant_a).get("/api/v1/org/settings/").status_code == 402

    # Cache invalidation runs on_commit; execute it so the paywall sees "active".
    with django_capture_on_commit_callbacks(execute=True):
        resp = staff_client.patch(
            f"/api/v1/platform/subscriptions/{sub.pk}/", {"status": "active"}, format="json"
        )
    assert resp.status_code == 200
    assert Subscription.objects.get(pk=sub.pk).status == "active"
    # Reactivated → no longer paywalled.
    assert client_for(tenant_a).get("/api/v1/org/settings/").status_code != 402


def test_subscription_non_numeric_id_404(staff_client, tenant_a):
    assert staff_client.get("/api/v1/platform/subscriptions/abc/").status_code == 404


# ---------------------------------------------------------------------------
# D4-LE-4/5 — impersonation: mint, both-sides audit, GET works, write 403,
# expired 401, no refresh
# ---------------------------------------------------------------------------
def _mint_impersonation(staff_client, tenant, user_id):
    return staff_client.post(
        f"/api/v1/platform/centers/{tenant.pk}/impersonate/", {"user_id": user_id}, format="json"
    )


def test_impersonation_mint_returns_access_only(staff_client, tenant_a, user_in):
    target = user_in(tenant_a, roles=["teacher"])
    resp = _mint_impersonation(staff_client, tenant_a, target.pk)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["expires_in"] == 600
    assert "access" in body  # access-ONLY...
    assert "refresh" not in body  # ...no refresh token is minted


def test_impersonation_unknown_user_404(staff_client, tenant_a):
    resp = _mint_impersonation(staff_client, tenant_a, 999999)
    assert resp.status_code == 404
    assert resp.json()["code"] == "user_not_found"


def test_impersonation_both_sides_audited(staff_client, tenant_a, user_in):
    from apps.tenancy.models import PlatformEvent

    target = user_in(tenant_a, roles=["teacher"])
    before_pe = PlatformEvent.objects.filter(event="impersonation.minted").count()

    resp = _mint_impersonation(staff_client, tenant_a, target.pk)
    assert resp.status_code == 200

    # Exactly one new PlatformEvent (public schema).
    after_pe = PlatformEvent.objects.filter(event="impersonation.minted").count()
    assert after_pe == before_pe + 1

    # Exactly one tenant-schema AuditLog "impersonation.started" with the impersonator.
    with schema_context(tenant_a.schema_name):
        from apps.audit.models import AuditLog

        rows = AuditLog.objects.filter(action="impersonation.started", resource_id=str(target.pk))
        assert rows.count() == 1
        assert rows.first().after.get("read_only") is True


def test_impersonation_token_get_works_write_denied(staff_client, tenant_a, user_in, client_for):
    """End-to-end: the minted token reads tenant data via a TenantSafeModelViewSet
    (200) but is denied on a write (403 read_only_token). Requires the core
    impersonation wiring (DenyWriteForReadOnlyToken on TenantSafeModelViewSet)."""
    from core import permissions as core_perms

    if not hasattr(core_perms, "DenyWriteForReadOnlyToken"):
        pytest.skip("DenyWriteForReadOnlyToken not wired yet (see integration_needed)")

    director = user_in(tenant_a, roles=["director"])
    minted = _mint_impersonation(staff_client, tenant_a, director.pk).json()["data"]
    token = minted["access"]

    tclient = client_for(tenant_a)
    tclient.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    # GET a tenant resource (TenantSafeModelViewSet) → 200 (read under impersonation).
    read = tclient.get("/api/v1/org/branches/")
    assert read.status_code == 200

    # Any write → 403 with the read_only_token code.
    write = tclient.post(
        "/api/v1/org/branches/",
        {"name": "ShouldFail", "slug": "should-fail", "address": "x"},
        format="json",
    )
    assert write.status_code == 403
    assert write.json()["code"] == "read_only_token"  # org/branches is now a layered view


def test_impersonation_write_denied_on_apiview(staff_client, tenant_a, user_in, client_for):
    """The read-only deny must also cover TenantSafeAPIView writes (enforced in
    initial(), not only via the ModelViewSet permission class) — many APIViews
    override permission_classes, so a permission-class-only guard would miss them.
    Uses the CenterSettings APIView: GET works, PATCH is denied read_only_token."""
    director = user_in(tenant_a, roles=["director"])
    token = _mint_impersonation(staff_client, tenant_a, director.pk).json()["data"]["access"]

    tclient = client_for(tenant_a)
    tclient.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    assert tclient.get("/api/v1/org/settings/").status_code == 200  # read under impersonation
    write = tclient.patch("/api/v1/org/settings/", {"late_threshold_minutes": 15}, format="json")
    assert write.status_code == 403
    assert write.json()["code"] == "read_only_token"  # org/settings is now a layered view


def test_impersonation_token_get_works_unconditionally(staff_client, tenant_a, user_in, client_for):
    """Even WITHOUT the write-deny wiring, the TD-1 auth class already accepts the
    impersonation token (it carries valid schema+tv), so a GET succeeds. This
    test proves the read path independent of the core write-deny wiring."""
    director = user_in(tenant_a, roles=["director"])
    token = _mint_impersonation(staff_client, tenant_a, director.pk).json()["data"]["access"]
    tclient = client_for(tenant_a)
    tclient.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert tclient.get("/api/v1/org/branches/").status_code == 200


def test_impersonation_token_expires(staff_client, tenant_a, user_in, client_for):
    """A token minted >10 min in the past is rejected 401 (no refresh to renew)."""
    import time_machine

    director = user_in(tenant_a, roles=["director"])
    with time_machine.travel(timezone.now() - timedelta(minutes=11), tick=False):
        token = _mint_impersonation(staff_client, tenant_a, director.pk).json()["data"]["access"]

    tclient = client_for(tenant_a)
    tclient.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert tclient.get("/api/v1/org/branches/").status_code == 401


def test_impersonation_mints_a_readonly_session(staff_client, tenant_a, user_in):
    """Impersonation issues an opaque READ-ONLY session in the target center's schema
    (custom session auth) — tenant-bound, read_only, short-lived, for the target user."""
    from django.utils import timezone
    from django_tenants.utils import schema_context

    from apps.users.models import Session

    target = user_in(tenant_a, roles=["teacher"])

    key = _mint_impersonation(staff_client, tenant_a, target.pk).json()["data"]["access"]

    with schema_context(tenant_a.schema_name):
        session = Session.objects.get(key=key)
        assert session.user_id == target.pk
        assert session.read_only is True
        assert session.revoked_at is None
        # Short-lived (cannot be extended) — far under the default multi-day TTL.
        assert session.expires_at < timezone.now() + timedelta(hours=1)


# ---------------------------------------------------------------------------
# D4-LE-5 — every lifecycle / subscription mutation writes a PlatformEvent
# ---------------------------------------------------------------------------
def test_lifecycle_mutations_write_platform_events(staff_client, tenant_a):
    from apps.tenancy.models import PlatformEvent

    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    staff_client.post(f"/api/v1/platform/centers/{tenant_a.pk}/suspend/", {}, format="json")
    staff_client.post(f"/api/v1/platform/centers/{tenant_a.pk}/activate/", {}, format="json")
    staff_client.post(f"/api/v1/platform/centers/{tenant_a.pk}/extend-trial/", {"days": 7}, format="json")
    events = set(PlatformEvent.objects.filter(center=tenant_a).values_list("event", flat=True))
    assert "center.suspended" in events
    assert "center.activated" in events
    assert "center.trial_extended" in events


def test_subscription_change_writes_platform_event(staff_client, tenant_a):
    from apps.tenancy.models import PlatformEvent

    sub = _set_subscription(tenant_a, status=Subscription.Status.SUSPENDED)
    before = PlatformEvent.objects.filter(event="subscription.changed").count()
    staff_client.patch(f"/api/v1/platform/subscriptions/{sub.pk}/", {"status": "active"}, format="json")
    after = PlatformEvent.objects.filter(event="subscription.changed").count()
    assert after == before + 1


def test_platform_event_is_append_only(staff_client, tenant_a):
    """No update/delete API surface exists for PlatformEvent (D4-LE-5)."""
    from apps.tenancy.models import PlatformEvent

    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    staff_client.post(f"/api/v1/platform/centers/{tenant_a.pk}/suspend/", {}, format="json")
    ev = PlatformEvent.objects.filter(center=tenant_a).first()
    assert ev is not None
    # There is no /platform/events/ route to mutate it.
    assert staff_client.delete(f"/api/v1/platform/events/{ev.pk}/").status_code in (404, 405)


# ---------------------------------------------------------------------------
# D4-LE-6 — TD-19 resolve (AllowAny, anon-throttled)
# ---------------------------------------------------------------------------
def test_resolve_happy(public_tenant, tenant_a, api_client):
    resp = api_client.get(f"/api/v1/platform/resolve/?slug={tenant_a.slug}")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["name"] == tenant_a.name
    assert body["ws_url"].endswith("/ws/notifications/")
    assert body["locale"]  # non-empty


def test_resolve_unknown_slug_404(public_tenant, api_client):
    resp = api_client.get("/api/v1/platform/resolve/?slug=does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["code"] == "center_not_found"


def test_resolve_missing_slug_400(public_tenant, api_client):
    assert api_client.get("/api/v1/platform/resolve/").status_code == 400


def test_resolve_anon_throttle(public_tenant, tenant_a, api_client, monkeypatch):
    """Anonymous resolve is anon-rate throttled (429 after the limit).

    The layered resolve view uses core.ratelimit.check_rate keyed by client IP
    (not DRF's AnonRateThrottle). Lower the view's per-IP limit so the 429 path
    fires within a few requests; conftest._clear_cache gives a fresh bucket.
    """
    from apps.tenancy.views.v1 import tenancy_views

    monkeypatch.setattr(tenancy_views, "RESOLVE_RATE_LIMIT", 2)
    url = f"/api/v1/platform/resolve/?slug={tenant_a.slug}"
    codes = [api_client.get(url).status_code for _ in range(4)]
    assert 429 in codes


# ---------------------------------------------------------------------------
# D4-LE-7 — apex admin lockdown: tenant-schema creds rejected (TD-3)
# ---------------------------------------------------------------------------
def test_apex_admin_rejects_tenant_schema_creds(public_tenant, tenant_a):
    """A user that exists ONLY in a tenant schema cannot log into apex /admin/
    (the public users table has no such row)."""
    from django.test import Client

    from apps.users.models import User

    with schema_context(tenant_a.schema_name):
        User.objects.create_superuser(username="tenant-only-admin", password=PASSWORD)

    client = Client()  # host "testserver" → public schema
    resp = client.post(
        "/admin/login/",
        {"username": "tenant-only-admin", "password": PASSWORD, "next": "/admin/"},
    )
    # Login fails (re-renders the form 200 with errors, never a 302 redirect).
    assert resp.status_code == 200
    assert not resp.wsgi_request.user.is_authenticated


def test_apex_admin_accepts_public_staff(platform_admin):
    from django.test import Client

    client = Client()
    resp = client.post("/admin/login/", {"username": "ctl-padmin", "password": PASSWORD, "next": "/admin/"})
    assert resp.status_code == 302
    assert resp.wsgi_request.user.is_authenticated
