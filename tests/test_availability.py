"""Durable fault isolation: one app can be disabled without taking down unrelated APIs.

The database is authoritative and Redis is only the per-request cache. Tests cover dependency
degradation, cache loss, transaction rollback, and control-plane self-lockout prevention.
"""

from __future__ import annotations

from collections.abc import Hashable

import pytest
from django.core.cache import cache
from django.db import DatabaseError
from django_tenants.utils import schema_context

from core.permissions import Role

# These tests exercise transaction.on_commit cache publication. They need real
# commits rather than pytest-django's outer rollback-only TestCase transaction.
pytestmark = pytest.mark.django_db(transaction=True)


def _disable(tenant, apps):
    from core.availability import set_tenant_disabled_apps

    with schema_context(tenant.schema_name):
        set_tenant_disabled_apps(set(apps))


# --- resolve_status (pure logic) ------------------------------------------
def test_resolve_status_transitive(tenant_a):
    from core.availability import (
        STATUS_DEGRADED,
        STATUS_DISABLED,
        STATUS_UNAVAILABLE,
        STATUS_UP,
        resolve_status,
    )

    cache.clear()
    with schema_context(tenant_a.schema_name):
        assert resolve_status("finance")[0] == STATUS_UP  # nothing disabled
    _disable(tenant_a, {"approvals"})
    with schema_context(tenant_a.schema_name):
        assert resolve_status("approvals")[0] == STATUS_DISABLED
        assert resolve_status("finance")[0] == STATUS_UNAVAILABLE  # hard dep down
        assert resolve_status("cohorts")[0] == STATUS_UP  # unrelated app unaffected
    _disable(tenant_a, {"notifications"})
    with schema_context(tenant_a.schema_name):
        status, warnings = resolve_status("attendance")  # soft dep down
        assert status == STATUS_DEGRADED
        assert any("notifications" in w for w in warnings)


# --- HTTP integration -----------------------------------------------------
def test_disabled_app_503s_and_others_keep_working(tenant_a, as_role):
    cache.clear()
    director, _ = as_role(Role.DIRECTOR)
    _disable(tenant_a, {"placement"})
    down = director.get("/api/v1/placement/tests/")
    assert down.status_code == 503
    body = down.json()
    assert body["success"] is False
    assert body["code"] == "service_unavailable"
    # a different app is completely unaffected — the project did NOT fall
    assert director.get("/api/v1/cohorts/").status_code == 200


def test_hard_dependency_down_makes_dependent_app_unavailable(tenant_a, as_role):
    cache.clear()
    director, _ = as_role(Role.DIRECTOR)
    _disable(tenant_a, {"approvals"})  # finance hard-depends on the A-1 approvals engine
    assert director.get("/api/v1/finance/invoices/").status_code == 503
    assert director.get("/api/v1/cohorts/").status_code == 200  # unrelated app fine


def test_soft_dependency_down_degrades_with_warnings(tenant_a, as_role):
    cache.clear()
    director, _ = as_role(Role.DIRECTOR)
    _disable(tenant_a, {"notifications"})  # attendance soft-depends on notifications
    r = director.get("/api/v1/attendance/records/")
    assert r.status_code == 200  # still works
    body = r.json()
    assert body["warnings"] == [
        {
            "code": "information_delayed",
            "message": "Some information may be delayed.",
            "affected_sections": ["attendance"],
        }
    ]


def test_control_endpoint_lists_and_toggles(tenant_a, as_role):
    cache.clear()
    # This module uses real commits against a shared tenant fixture. Establish
    # the durable precondition instead of relying on an earlier test's policy.
    _disable(tenant_a, set())
    director, _ = as_role(Role.DIRECTOR)
    listing = director.get("/api/v1/org/system/apps/")
    assert listing.status_code == 200
    apps = {a["app"]: a["status"] for a in listing.json()["data"]["apps"]}
    assert apps.get("finance") == "up"

    patched = director.patch("/api/v1/org/system/apps/", {"disabled": ["placement"]}, format="json")
    assert patched.status_code == 200
    assert "placement" in patched.json()["data"]["disabled"]
    # the toggle took effect immediately (no restart)
    assert director.get("/api/v1/placement/tests/").status_code == 503
    # ...and re-enabling brings it back
    director.patch("/api/v1/org/system/apps/", {"disabled": []}, format="json")
    assert director.get("/api/v1/placement/tests/").status_code == 200


def test_control_endpoint_rejects_a_bad_body(tenant_a, as_role):
    cache.clear()
    director, _ = as_role(Role.DIRECTOR)
    r = director.patch("/api/v1/org/system/apps/", {"disabled": "placement"}, format="json")
    assert r.status_code == 400


def test_disabled_apps_persist_across_cache_loss_and_model_writes_invalidate(tenant_a):
    from apps.org.models import CenterSettings
    from core.availability import _cache_key, disabled_apps

    cache.clear()
    _disable(tenant_a, {"placement"})
    with schema_context(tenant_a.schema_name):
        key = _cache_key()
        assert CenterSettings.objects.get(pk=1).disabled_apps == ["placement"]
        assert disabled_apps() == {"placement"}

        cache.clear()
        assert disabled_apps() == {"placement"}

        settings_row = CenterSettings.objects.get(pk=1)
        settings_row.disabled_apps = ["notifications"]
        settings_row.save(update_fields=("disabled_apps", "updated_at"))
        assert cache.get(key) is None
        assert disabled_apps() == {"notifications"}


def test_stale_policy_cache_converges_after_failed_outage_invalidation(tenant_a, monkeypatch):
    """A Redis outage can lose an invalidation, but never preserve old policy forever."""
    from core import availability

    class RecoveringCache:
        def __init__(self) -> None:
            self.available = True
            self.now = 0
            self.entries: dict[Hashable, tuple[object, int]] = {}

        def get(self, key, default=None):
            if not self.available:
                raise ConnectionError("cache unavailable")
            entry = self.entries.get(key)
            if entry is None:
                return default
            value, expires_at = entry
            if self.now >= expires_at:
                self.entries.pop(key, None)
                return default
            return value

        def set(self, key, value, timeout=None):
            if not self.available:
                raise ConnectionError("cache unavailable")
            assert isinstance(timeout, int)
            assert 1 <= timeout <= 300
            self.entries[key] = (value, self.now + timeout)
            return True

        def delete_many(self, keys):
            if not self.available:
                raise ConnectionError("cache unavailable")
            for key in keys:
                self.entries.pop(key, None)

        def advance(self, seconds: int) -> None:
            self.now += seconds

    recovering_cache = RecoveringCache()
    durable_policy: set[str] = set()
    monkeypatch.setattr(availability, "cache", recovering_cache)
    monkeypatch.setattr(availability, "_database_disabled_apps", lambda: set(durable_policy))

    with schema_context(tenant_a.schema_name):
        key = availability._cache_key()
        assert availability.disabled_apps() == set()

        # PostgreSQL commits a stricter policy while Redis is unavailable. The attempted
        # invalidation is lost, so recovery exposes the old cache entry temporarily.
        durable_policy.add("placement")
        recovering_cache.available = False
        with pytest.raises(ConnectionError, match="cache unavailable"):
            recovering_cache.delete_many((key,))
        recovering_cache.available = True
        assert availability.disabled_apps() == set()

        # The finite TTL forces a database reload and restores the durable policy without
        # any manual cache flush after Redis recovers.
        recovering_cache.advance(availability._cache_timeout_seconds())
        assert availability.disabled_apps() == {"placement"}
        assert recovering_cache.entries[key][1] > recovering_cache.now


def test_runtime_policy_publication_uses_finite_cache_ttl(tenant_a, monkeypatch):
    from core import availability

    writes: list[tuple[str, object, int | None]] = []

    def capture_set(key, value, timeout=None):
        writes.append((key, value, timeout))
        return True

    monkeypatch.setattr(availability.cache, "set", capture_set)
    with schema_context(tenant_a.schema_name):
        availability.set_tenant_disabled_apps({"placement"})

    assert writes
    assert writes[-1][1] == ["placement"]
    assert writes[-1][2] == availability._cache_timeout_seconds()


def test_disabled_apps_rollback_never_publishes_uncommitted_cache_state(tenant_a):
    from django.db import transaction

    from apps.org.models import CenterSettings
    from core.availability import _cache_key, disabled_apps, set_tenant_disabled_apps

    cache.clear()
    _disable(tenant_a, {"placement"})
    with schema_context(tenant_a.schema_name):
        key = _cache_key()
        assert cache.get(key) == ["placement"]

        def rollback_probe() -> None:
            with transaction.atomic():
                set_tenant_disabled_apps({"notifications"})
                raise RuntimeError("rollback probe")

        with pytest.raises(RuntimeError, match="rollback probe"):
            rollback_probe()

        assert CenterSettings.objects.get(pk=1).disabled_apps == ["placement"]
        assert cache.get(key) == ["placement"]
        assert disabled_apps() == {"placement"}


def test_foundational_apps_cannot_be_disabled(tenant_a, as_role):
    """Self-lockout guard: org/auth/users host the control plane + auth surface, and the
    toggle endpoint itself lives under /api/v1/org/ — disabling `org` would 503 the very
    endpoint needed to re-enable it. The API rejects it (400) and the control plane survives."""
    from core.availability import (
        PROTECTED_APPS,
        STATUS_DISABLED,
        _cache_key,
        resolve_status,
        set_tenant_disabled_apps,
    )

    cache.clear()
    director, _ = as_role(Role.DIRECTOR)
    # The API refuses to disable a protected app, with a clear error...
    r = director.patch("/api/v1/org/system/apps/", {"disabled": ["org"]}, format="json")
    assert r.status_code == 400
    # ...and the control endpoint is still reachable (NOT bricked).
    assert director.get("/api/v1/org/system/apps/").status_code == 200

    with schema_context(tenant_a.schema_name):
        # Direct call strips the protected set (defense in depth), keeps a real target.
        effective = set_tenant_disabled_apps({"org", "auth", "users", "placement"})
        assert PROTECTED_APPS.isdisjoint(effective)
        assert "placement" in effective
        # And even if a protected app somehow sits in the raw set (stale/global entry),
        # resolve_status never reports it disabled.
        cache.set(_cache_key(), ["org"], timeout=None)
        assert resolve_status("org")[0] != STATUS_DISABLED


def test_resolve_status_reads_disabled_set_once(tenant_a, monkeypatch):
    """The disabled set is read from cache exactly ONCE per resolve_status call, not once per
    node of the dependency-graph walk — the per-request hot path must not fan out into N Redis
    GETs. (payments -> finance,approvals,notifications; finance -> approvals,notifications.)"""
    from core import availability

    cache.clear()
    calls = {"n": 0}
    real = availability.disabled_apps

    def counting() -> set[str]:
        calls["n"] += 1
        return real()

    monkeypatch.setattr(availability, "disabled_apps", counting)
    with schema_context(tenant_a.schema_name):
        availability.resolve_status("payments")
    assert calls["n"] == 1


def test_database_policy_read_failure_disables_optional_apps_but_keeps_control_plane(
    tenant_a,
    monkeypatch,
):
    """A policy-store outage must fail closed without bricking its repair surface."""
    from apps.org.models import CenterSettings
    from core import availability

    def unavailable(*args, **kwargs):
        del args, kwargs
        raise DatabaseError("policy store unavailable")

    cache.clear()
    monkeypatch.setattr(CenterSettings.objects, "filter", unavailable)
    with schema_context(tenant_a.schema_name):
        disabled = availability.disabled_apps()

    assert disabled == set(availability.APP_MOUNTS.values()) - availability.PROTECTED_APPS
    assert availability.PROTECTED_APPS.isdisjoint(disabled)
