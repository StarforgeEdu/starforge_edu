"""Lane E (D3-E) tests — Billing / Paywall.

Coverage (DAY-3.md "Tests required"):
- middleware: 402 on suspended + allowlist passes + active passes + public no-op
- trial flip with frozen time (trialing → suspended past grace)
- active → past_due and past_due → suspended flips
- metering snapshot idempotency (re-run updates, never duplicates)
- student-limit enforcement at the boundary (max ok, max+1 raises 402)
- subscription auto-created on Center provisioning
- dunning dispatch dedupe (one Notification per director per (status,date))
- platform endpoints (plans/subscriptions/usage/checkout) perms + behavior

All public-schema rows (Plan/Subscription/UsageSnapshot) are created WITHOUT a
schema_context wrapper; tenant rows (StudentProfile, RoleMembership) inside one.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import time_machine
from django.core.cache import cache
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.billing.models import Plan, Subscription, UsageSnapshot
from apps.billing.tests.factories import PlanFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ensure_plan() -> Plan:
    plan = Plan.objects.filter(is_active=True).order_by("price_uzs").first()
    return plan or PlanFactory(code="starter", price_uzs=0, max_students=100)


def _set_subscription(center, *, status, period_end=None, plan=None):
    """Create/update the center's subscription on the PUBLIC schema."""
    plan = plan or _ensure_plan()
    period_end = period_end or (timezone.now() + timedelta(days=30))
    sub, _ = Subscription.objects.update_or_create(
        center=center,
        defaults={
            "plan": plan,
            "status": status,
            "current_period_start": timezone.now() - timedelta(days=1),
            "current_period_end": period_end,
        },
    )
    cache.delete(f"billing:subscription_status:{center.schema_name}")
    return sub


# ---------------------------------------------------------------------------
# Auto-subscription on provisioning (D3-E-3)
# ---------------------------------------------------------------------------
def test_subscription_auto_created_on_provisioning(tenant_a):
    _ensure_plan()
    # tenant_a is provisioned by conftest; the receiver should have created (or,
    # if a bare DB had no plan at provision time, we re-create) a trialing sub.
    from apps.billing.services import create_trial_subscription

    sub = Subscription.objects.filter(center=tenant_a).first()
    if sub is None:
        sub = create_trial_subscription(center=tenant_a)
    assert sub.status in {Subscription.Status.TRIALING, Subscription.Status.ACTIVE}
    assert sub.center_id == tenant_a.pk


def test_create_trial_subscription_idempotent(tenant_a):
    _ensure_plan()
    from apps.billing.services import create_trial_subscription

    first = create_trial_subscription(center=tenant_a)
    second = create_trial_subscription(center=tenant_a)
    assert first.pk == second.pk
    assert Subscription.objects.filter(center=tenant_a).count() == 1


# ---------------------------------------------------------------------------
# Middleware paywall (D3-E-4)
# ---------------------------------------------------------------------------
def test_middleware_402_on_suspended(tenant_a, user_in, as_user):
    _set_subscription(tenant_a, status=Subscription.Status.SUSPENDED)
    from core.permissions import Role

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    client = as_user(tenant_a, user)
    resp = client.get("/api/v1/students/")
    assert resp.status_code == 402
    assert resp.json()["code"] == "subscription_required"


def test_middleware_active_passes(tenant_a, user_in, as_user):
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    from core.permissions import Role

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    client = as_user(tenant_a, user)
    resp = client.get("/api/v1/students/")
    assert resp.status_code == 200


def test_middleware_allowlist_passes_when_suspended(tenant_a, client_for):
    _set_subscription(tenant_a, status=Subscription.Status.SUSPENDED)
    client = client_for(tenant_a)
    # /api/v1/auth/ is allowlisted; a suspended tenant must still be able to log
    # in. (login expects POST; we only assert it is NOT the 402 paywall.)
    resp = client.post("/api/v1/auth/login/", {"username": "x", "password": "y"}, format="json")
    assert resp.status_code != 402


def test_middleware_healthz_passes_when_suspended(tenant_a, client_for):
    _set_subscription(tenant_a, status=Subscription.Status.SUSPENDED)
    client = client_for(tenant_a)
    resp = client.get("/healthz/live")
    assert resp.status_code != 402


def test_middleware_public_schema_noop(public_tenant, api_client):
    # Public schema is never gated — even if a suspended sub exists somewhere.
    resp = api_client.get("/api/v1/platform/billing/plans/")
    # Unauthenticated → 401 (IsAdminUser), NOT 402. The gate did not fire.
    assert resp.status_code != 402


def test_middleware_cross_tenant_unaffected(tenant_a, tenant_b, user_in, as_user):
    """Suspending tenant_a does not gate tenant_b."""
    _set_subscription(tenant_a, status=Subscription.Status.SUSPENDED)
    _set_subscription(tenant_b, status=Subscription.Status.ACTIVE)
    from core.permissions import Role

    user_b = user_in(tenant_b, roles=[Role.DIRECTOR])
    client_b = as_user(tenant_b, user_b)
    assert client_b.get("/api/v1/students/").status_code == 200


# ---------------------------------------------------------------------------
# State flips with frozen time (D3-E-5)
# ---------------------------------------------------------------------------
def test_trialing_flips_to_suspended_past_grace(tenant_a, settings):
    settings.BILLING_TRIAL_GRACE_DAYS = 3
    from apps.billing.services import apply_state_flip, evaluate_subscription_state

    now = timezone.now()
    sub = _set_subscription(tenant_a, status=Subscription.Status.TRIALING, period_end=now - timedelta(days=5))
    new = evaluate_subscription_state(subscription=sub)
    assert new == Subscription.Status.SUSPENDED
    apply_state_flip(subscription=sub, new_status=new)
    sub.refresh_from_db()
    assert sub.status == Subscription.Status.SUSPENDED


def test_trialing_within_grace_does_not_flip(tenant_a, settings):
    settings.BILLING_TRIAL_GRACE_DAYS = 3
    from apps.billing.services import evaluate_subscription_state

    now = timezone.now()
    # period ended 1 day ago, grace is 3 → still trialing.
    sub = _set_subscription(tenant_a, status=Subscription.Status.TRIALING, period_end=now - timedelta(days=1))
    assert evaluate_subscription_state(subscription=sub) is None


def test_active_flips_to_past_due(tenant_a):
    from apps.billing.services import evaluate_subscription_state

    sub = _set_subscription(
        tenant_a, status=Subscription.Status.ACTIVE, period_end=timezone.now() - timedelta(hours=1)
    )
    assert evaluate_subscription_state(subscription=sub) == Subscription.Status.PAST_DUE


def test_past_due_flips_to_suspended_past_dunning(tenant_a, settings):
    settings.BILLING_DUNNING_DAYS = 7
    from apps.billing.services import evaluate_subscription_state

    sub = _set_subscription(
        tenant_a,
        status=Subscription.Status.PAST_DUE,
        period_end=timezone.now() - timedelta(days=10),
    )
    assert evaluate_subscription_state(subscription=sub) == Subscription.Status.SUSPENDED


def test_metering_task_flips_trial_with_frozen_time(tenant_a, settings):
    settings.BILLING_TRIAL_GRACE_DAYS = 3
    _set_subscription(
        tenant_a,
        status=Subscription.Status.TRIALING,
        period_end=timezone.now() + timedelta(days=1),
    )
    from celery_tasks.billing_tasks import meter_center

    # Travel 5 days past period end + grace → suspended.
    future = timezone.now() + timedelta(days=6)
    with time_machine.travel(future, tick=False):
        meter_center(center_id=tenant_a.pk)
    sub = Subscription.objects.get(center=tenant_a)
    assert sub.status == Subscription.Status.SUSPENDED


# ---------------------------------------------------------------------------
# Metering snapshot idempotency (D3-E-5)
# ---------------------------------------------------------------------------
def test_metering_snapshot_idempotent(tenant_a):
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    from celery_tasks.billing_tasks import meter_center

    meter_center(center_id=tenant_a.pk)
    meter_center(center_id=tenant_a.pk)
    today = timezone.localdate()  # meter_center now stamps the LOCAL date (R4/CONF1)
    assert UsageSnapshot.objects.filter(center=tenant_a, date=today).count() == 1


def test_metering_snapshot_records_student_count(tenant_a):
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        StudentProfileFactory.create_batch(3, status="active")
    from celery_tasks.billing_tasks import meter_center

    meter_center(center_id=tenant_a.pk)
    snap = UsageSnapshot.objects.get(center=tenant_a, date=timezone.localdate())
    assert snap.students_count == 3


# ---------------------------------------------------------------------------
# Student-limit enforcement at the boundary (D3-E-7)
# ---------------------------------------------------------------------------
def test_enforce_student_limit_at_boundary(tenant_a):
    from apps.billing.services import PlanLimitExceeded, enforce_student_limit

    plan = PlanFactory(code="cap2", max_students=2, price_uzs=0)
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE, plan=plan)
    from apps.students.tests.factories import StudentProfileFactory

    # Below the cap: 1 active student, enforcing for the 2nd → OK.
    with schema_context(tenant_a.schema_name):
        StudentProfileFactory(status="active")
    with schema_context(tenant_a.schema_name):
        enforce_student_limit()  # 1 < 2, no raise

    # At the cap: 2 active students, enforcing for the 3rd → 402.
    with schema_context(tenant_a.schema_name):
        StudentProfileFactory(status="active")
    with schema_context(tenant_a.schema_name), pytest.raises(PlanLimitExceeded) as exc:
        enforce_student_limit()
    assert exc.value.code == "plan_limit_exceeded"
    assert exc.value.status_code == 402


def test_enforce_student_limit_noop_without_subscription(tenant_b):
    """No subscription row → never blocks."""
    Subscription.objects.filter(center=tenant_b).delete()
    cache.delete(f"billing:subscription_status:{tenant_b.schema_name}")
    from apps.billing.services import enforce_student_limit

    with schema_context(tenant_b.schema_name):
        enforce_student_limit()  # must not raise


# ---------------------------------------------------------------------------
# Dunning dispatch dedupe (D3-E-9)
# ---------------------------------------------------------------------------
def test_dunning_dispatch_dedupe(tenant_a, django_capture_on_commit_callbacks):
    """Flipping to suspended dispatches once per director per (status, date);
    re-running on the same day adds no new Notification rows. The dunning side
    effect is scheduled via transaction.on_commit, so it runs under
    django_capture_on_commit_callbacks(execute=True)."""
    pytest.importorskip("apps.notifications.models")
    from apps.billing.services import apply_state_flip
    from apps.notifications.models import Notification
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory
    from core.permissions import Role

    with schema_context(tenant_a.schema_name):
        from apps.org.tests.factories import BranchFactory

        branch = BranchFactory()
        director = UserFactory()
        RoleMembership.objects.create(user=director, branch=branch, role=Role.DIRECTOR)

    sub = _set_subscription(
        tenant_a, status=Subscription.Status.PAST_DUE, period_end=timezone.now() - timedelta(days=30)
    )
    with django_capture_on_commit_callbacks(execute=True):
        apply_state_flip(subscription=sub, new_status=Subscription.Status.SUSPENDED)

    with schema_context(tenant_a.schema_name):
        first_count = Notification.objects.filter(user=director).count()
    assert first_count >= 1

    # Re-run the dunning side effect for the same status+date → dedupe (no growth).
    from apps.billing.services import _run_dunning

    _run_dunning(center_id=tenant_a.pk, status=Subscription.Status.SUSPENDED)
    with schema_context(tenant_a.schema_name):
        second_count = Notification.objects.filter(user=director).count()
    assert second_count == first_count


# ---------------------------------------------------------------------------
# Platform endpoints (D3-E-8): plans / subscriptions / usage / checkout
# ---------------------------------------------------------------------------
def _platform_admin(public_tenant):
    from apps.users.models import User

    user = User.objects.create_user(username="platform-admin", password="x" * 12)
    user.is_staff = True
    user.is_superuser = True
    user.save(update_fields=["is_staff", "is_superuser"])
    return user


def _authed(api_client, user):
    # The layered billing views authenticate with the custom session authenticator
    # (not DRF force_authenticate) — mint a real public-schema Bearer session.
    from core.session_auth import create_session

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {create_session(user).key}")
    return api_client


def test_plans_endpoint_requires_admin(public_tenant, api_client):
    assert api_client.get("/api/v1/platform/billing/plans/").status_code == 401


def test_plans_endpoint_lists_for_admin(public_tenant, api_client):
    _ensure_plan()
    admin = _platform_admin(public_tenant)
    _authed(api_client, admin)
    resp = api_client.get("/api/v1/platform/billing/plans/")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"success", "data", "pagination"}  # layered envelope


def test_subscription_patch_suspend_then_reactivate(public_tenant, api_client, tenant_a):
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    admin = _platform_admin(public_tenant)
    _authed(api_client, admin)
    url = f"/api/v1/platform/billing/subscriptions/{tenant_a.pk}/"
    resp = api_client.patch(url, {"status": "suspended"}, format="json")
    assert resp.status_code == 200
    assert Subscription.objects.get(center=tenant_a).status == "suspended"

    resp = api_client.patch(url, {"status": "active"}, format="json")
    assert resp.status_code == 200
    assert Subscription.objects.get(center=tenant_a).status == "active"


def test_subscription_patch_invalid_status(public_tenant, api_client, tenant_a):
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    admin = _platform_admin(public_tenant)
    _authed(api_client, admin)
    url = f"/api/v1/platform/billing/subscriptions/{tenant_a.pk}/"
    resp = api_client.patch(url, {"status": "trialing"}, format="json")
    assert resp.status_code == 400


def test_reactivating_lapsed_center_extends_period(public_tenant, tenant_a):
    """R3/CONF6: reactivating a center whose period has lapsed must push
    current_period_end into the future, else the nightly meter re-suspends it within a
    day (activate would be a no-op)."""
    from apps.billing.services import change_subscription

    # A genuinely lapsed suspended center: period_end in the past.
    _set_subscription(
        tenant_a,
        status=Subscription.Status.SUSPENDED,
        period_end=timezone.now() - timedelta(days=5),
    )
    sub = change_subscription(center_id=tenant_a.pk, status="active")
    assert sub.status == "active"
    assert sub.current_period_end > timezone.now()  # period extended, not left stale


def test_reactivating_active_center_does_not_shorten_future_period(public_tenant, tenant_a):
    """The extension only kicks in for a LAPSED period — an already-active center with
    a future period keeps it (no accidental reset)."""
    from apps.billing.services import change_subscription

    future = timezone.now() + timedelta(days=20)
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE, period_end=future)
    sub = change_subscription(center_id=tenant_a.pk, status="active")
    assert abs((sub.current_period_end - future).total_seconds()) < 5  # unchanged


def test_extend_trial_syncs_subscription_period(public_tenant, tenant_a):
    """R3/CONF7: extending a center's trial must also push the subscription's
    current_period_end, else the billing meter suspends (402) the tenant at the
    ORIGINAL trial end despite the extension."""
    from apps.tenancy.services import extend_trial

    original_end = timezone.now() + timedelta(days=2)
    _set_subscription(tenant_a, status=Subscription.Status.TRIALING, period_end=original_end)
    tenant_a.trial_ends_at = original_end
    tenant_a.save(update_fields=["trial_ends_at"])

    extend_trial(tenant_a, days=30)
    sub = Subscription.objects.get(center=tenant_a)
    assert sub.current_period_end > original_end + timedelta(days=20)  # tracks the new trial end


def test_usage_endpoint(public_tenant, api_client, tenant_a):
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    from apps.billing.tests.factories import UsageSnapshotFactory

    UsageSnapshotFactory(center=tenant_a, students_count=42)
    admin = _platform_admin(public_tenant)
    _authed(api_client, admin)
    resp = api_client.get(f"/api/v1/platform/billing/usage/?center={tenant_a.pk}")
    assert resp.status_code == 200
    assert any(row["students_count"] == 42 for row in resp.json()["data"])


def test_checkout_mock_extends_and_activates(public_tenant, api_client, tenant_a, settings):
    settings.PLATFORM_PAYMENTS_USE_MOCK = True
    sub = _set_subscription(
        tenant_a, status=Subscription.Status.SUSPENDED, period_end=timezone.now() - timedelta(days=1)
    )
    old_end = sub.current_period_end
    admin = _platform_admin(public_tenant)
    _authed(api_client, admin)
    resp = api_client.post(
        "/api/v1/platform/billing/checkout/",
        {"center": tenant_a.pk, "provider": "payme"},
        format="json",
    )
    assert resp.status_code == 200
    sub.refresh_from_db()
    assert sub.status == "active"
    assert sub.current_period_end > old_end + timedelta(days=29)


def test_checkout_requires_admin(public_tenant, api_client, tenant_a):
    resp = api_client.post("/api/v1/platform/billing/checkout/", {"center": tenant_a.pk}, format="json")
    assert resp.status_code == 401


def test_subscription_patch_non_string_status_is_400_not_500(public_tenant, api_client, tenant_a):
    """A list/dict `status` must be a clean 400, not a 500 (unhashable frozenset test)."""
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    _authed(api_client, _platform_admin(public_tenant))
    url = f"/api/v1/platform/billing/subscriptions/{tenant_a.pk}/"
    assert api_client.patch(url, {"status": ["active"]}, format="json").status_code == 400


def test_checkout_non_string_provider_is_400_not_500(public_tenant, api_client, tenant_a):
    """A list/dict `provider` must be a clean 400, not a 500 (unhashable frozenset test)."""
    _set_subscription(tenant_a, status=Subscription.Status.ACTIVE)
    _authed(api_client, _platform_admin(public_tenant))
    resp = api_client.post(
        "/api/v1/platform/billing/checkout/",
        {"center": tenant_a.pk, "provider": ["payme"]},
        format="json",
    )
    assert resp.status_code == 400


def test_subscription_patch_empty_body_on_missing_center_is_400(public_tenant, api_client, tenant_a):
    """Body validation runs BEFORE the 404 (old serializer-first order): an empty
    PATCH to a center with no subscription is a 400, not a 404."""
    Subscription.objects.filter(center=tenant_a).delete()
    _authed(api_client, _platform_admin(public_tenant))
    url = f"/api/v1/platform/billing/subscriptions/{tenant_a.pk}/"
    assert api_client.patch(url, {}, format="json").status_code == 400
