"""App-level availability + graceful degradation (fault isolation).

Every feature app can be turned OFF at runtime, and a down app never takes the whole API
with it:

* A **disabled** app's endpoints answer a clean ``503 service_unavailable`` JSON — never a
  crash — so every OTHER app keeps serving normally.
* Apps declare their dependencies. A **hard** dependency being down makes the app
  ``unavailable`` (503, naming what's down). A **soft** dependency being down leaves the app
  ``up`` but **degraded** — it still works, and its JSON response carries a ``warnings`` list
  naming the degraded dependency (so a caller knows e.g. notifications aren't being sent).

Toggling is CONTROLLABLE without a restart: the disabled set is the union of the
``DISABLED_APPS`` setting (ops default) and a cache-backed override a director can change via
the system-status endpoint. Resolution is a cheap in-memory graph walk + one cache read, so
it adds negligible latency.

This module is pure logic (no Django models); the ``AppAvailabilityMiddleware`` and the
``/api/v1/org/system/`` endpoints drive it.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache

# URL mount (/api/v1/<mount>/) -> app label. Most mounts equal the label; the exceptions
# are listed explicitly. Anything not here is treated as an unmanaged path (always up).
APP_MOUNTS: dict[str, str] = {
    "auth": "auth", "users": "users", "org": "org", "students": "students",
    "parents": "parents", "teachers": "teachers", "cohorts": "cohorts",
    "schedule": "schedule", "attendance": "attendance", "academics": "academics",
    "assignments": "assignments", "content": "content", "printing": "printing",
    "finance": "finance", "payments": "payments", "notifications": "notifications",
    "ai": "ai", "audit": "audit", "reports": "reports", "approvals": "approvals",
    "rulebook": "compliance", "access": "access", "forms": "forms", "tasks": "staff_tasks",
    "messaging": "messaging", "intelligence": "intelligence", "achievements": "achievements",
    "rewards": "rewards", "cover": "covers", "loans": "loans", "procurement": "procurement",
    "campaigns": "campaigns", "sales": "sales", "meetings": "meetings",
    "placement": "placement", "cards": "cards",
}

# The dependency graph. HARD = the app cannot function without it (down -> 503). SOFT = the
# app degrades but keeps working (down -> a warning on the response). Deliberately
# CONSERVATIVE: only architecturally-required edges are HARD (a wrong hard edge would 503 a
# still-usable app); everything else is SOFT so the default is "keeps working". Foundational
# apps (auth/users/org/tenancy/approvals/audit/access) have no deps — they ARE the base and
# should not normally be disabled. Extend freely; a missing app defaults to no deps.
APP_DEPENDENCIES: dict[str, dict[str, list[str]]] = {
    # money spine — the A-1 approvals/ledger engine is a hard requirement
    "finance": {"hard": ["approvals"], "soft": ["notifications"]},
    "payments": {"hard": ["finance", "approvals"], "soft": ["notifications"]},
    "loans": {"hard": ["approvals"], "soft": []},
    "sales": {"hard": ["approvals"], "soft": []},
    "procurement": {"hard": ["approvals"], "soft": []},
    "rewards": {"hard": ["approvals"], "soft": []},
    # attendance is keyed to schedule.Lesson + cohort membership
    "attendance": {"hard": ["schedule", "cohorts"], "soft": ["notifications", "cards"]},
    "covers": {"hard": ["schedule"], "soft": []},
    "assignments": {"hard": ["cohorts"], "soft": ["notifications"]},
    "academics": {"hard": ["cohorts"], "soft": []},
    "content": {"hard": ["cohorts"], "soft": []},
    "schedule": {"hard": ["cohorts"], "soft": ["notifications"]},
    # teacher payroll rides the A-1 engine, but the rest of teachers works without it
    "teachers": {"hard": [], "soft": ["approvals"]},
    "cards": {"hard": [], "soft": ["attendance"]},
    "parents": {"hard": ["students"], "soft": []},
    # AI-backed features degrade to manual when ai is off
    "placement": {"hard": [], "soft": ["ai"]},
    "campaigns": {"hard": [], "soft": ["ai", "notifications"]},
    "forms": {"hard": [], "soft": ["ai"]},
    # read-only aggregations degrade rather than fail
    "reports": {"hard": [], "soft": ["finance", "attendance", "academics"]},
    "intelligence": {"hard": [], "soft": ["finance", "attendance", "academics"]},
    "messaging": {"hard": [], "soft": ["notifications"]},
}

_DISABLED_CACHE_PREFIX = "core:availability:disabled_apps"

STATUS_UP = "up"
STATUS_DEGRADED = "degraded"
STATUS_DISABLED = "disabled"
STATUS_UNAVAILABLE = "unavailable"

# Apps that host the tenant control plane + auth surface. They can NEVER be turned off by the
# per-tenant runtime toggle: the availability control endpoint itself lives under
# ``/api/v1/org/``, so disabling ``org`` (or ``auth``/``users``) would 503 the very endpoint
# needed to re-enable apps — an unrecoverable self-lockout. Guarded in both
# ``set_tenant_disabled_apps`` (can't be added to the set) and ``resolve_status`` (never
# resolves to disabled, even if a stale/global entry names one), so the control plane always
# stays reachable.
PROTECTED_APPS: frozenset[str] = frozenset({"auth", "users", "org"})


def _global_disabled() -> frozenset[str]:
    """Ops-level disables (the DISABLED_APPS setting) — apply to EVERY tenant and can't be
    re-enabled per-tenant (a broken app is off everywhere)."""
    return frozenset(getattr(settings, "DISABLED_APPS", ()) or ())


def _cache_key() -> str:
    from core.utils import current_schema

    return f"{_DISABLED_CACHE_PREFIX}:{current_schema()}"


def disabled_apps() -> set[str]:
    """The apps off for the CURRENT tenant: the global ops default UNION this tenant's
    runtime override (a director toggling apps, no restart needed)."""
    override = cache.get(_cache_key()) or ()
    return set(_global_disabled()) | set(override)


def set_tenant_disabled_apps(apps: set[str]) -> set[str]:
    """Persist THIS tenant's disabled set (runtime toggle). Only known app labels are kept
    (a typo can't silently disable everything), foundational ``PROTECTED_APPS`` are stripped
    (disabling them would brick the control plane itself), and a globally-disabled app is
    implicitly included. Returns the resulting effective disabled set."""
    known = set(APP_MOUNTS.values())
    tenant_set = sorted((set(apps) & known) - PROTECTED_APPS)
    cache.set(_cache_key(), tenant_set, timeout=None)
    return set(tenant_set) | set(_global_disabled())


def app_for_mount(mount: str) -> str | None:
    """The app label a URL mount belongs to (None if the mount is unmanaged)."""
    return APP_MOUNTS.get(mount)


def resolve_status(app: str, _seen: frozenset[str] = frozenset()) -> tuple[str, list[str]]:
    """(status, warnings) for ``app``. Transitive: an app whose HARD dep resolves to
    disabled/unavailable is itself ``unavailable``; a SOFT dep that is disabled/unavailable
    downgrades it to ``degraded`` with a warning. Cycle-safe via ``_seen``.

    The disabled set is read from cache exactly ONCE and threaded through the whole graph
    walk (see ``_resolve``) — resolving a dep chain must not fan out into N Redis reads on
    the per-request hot path."""
    return _resolve(app, disabled_apps(), _seen)


def _resolve(app: str, disabled: set[str], seen: frozenset[str]) -> tuple[str, list[str]]:
    """The recursion for :func:`resolve_status`, over a pre-fetched ``disabled`` set (no I/O)."""
    if app in disabled and app not in PROTECTED_APPS:
        return STATUS_DISABLED, [f"The {app} service is turned off."]
    if app in seen:  # a dependency cycle — treat as up to avoid infinite recursion
        return STATUS_UP, []
    seen = seen | {app}
    deps = APP_DEPENDENCIES.get(app, {})
    warnings: list[str] = []
    for hard in deps.get("hard", []):
        h_status, _ = _resolve(hard, disabled, seen)
        if h_status in (STATUS_DISABLED, STATUS_UNAVAILABLE):
            return STATUS_UNAVAILABLE, [f"The {app} service is unavailable: it requires {hard}, which is down."]
    for soft in deps.get("soft", []):
        s_status, _ = _resolve(soft, disabled, seen)
        if s_status in (STATUS_DISABLED, STATUS_UNAVAILABLE):
            warnings.append(f"{soft} is down — {app} is running in a degraded mode.")
    return (STATUS_DEGRADED if warnings else STATUS_UP), warnings


def system_status() -> list[dict]:
    """A snapshot of every managed app's status — for the system-status endpoint. Reads the
    disabled set once and reuses it across all ~38 apps (not one Redis read per app)."""
    disabled = disabled_apps()
    out = []
    for app in sorted(set(APP_MOUNTS.values())):
        status, warnings = _resolve(app, disabled, frozenset())
        out.append({"app": app, "status": status, "warnings": warnings})
    return out
