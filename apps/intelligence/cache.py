"""Permission-safe stale-while-revalidate caching for intelligence reads.

The cache is deliberately an optimization rather than an authorization source.
Views resolve and validate the live principal, effective grants, and row scope
before constructing a key or accepting a cached payload.  Redis failures always
fall through to the authoritative loader.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from django.http import HttpResponse

from core.permissions import get_effective_permission_context
from core.role_principals import RolePrincipal
from core.utils import current_schema, stable_hash

CacheStatus = Literal["hit", "miss", "stale", "refresh", "bypass", "disabled"]

_ENTRY_VERSION = 1
_FUTURE_CLOCK_TOLERANCE_SECONDS = 5.0


class CacheBackend(Protocol):
    """The small subset shared by Django's Redis and local-memory caches."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def add(self, key: str, value: Any, timeout: int | None = None) -> bool: ...

    def set(self, key: str, value: Any, timeout: int | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """Fresh/stale retention and a bounded distributed refresh lease."""

    fresh_seconds: int
    stale_seconds: int
    lock_seconds: int = 60

    def __post_init__(self) -> None:
        disabled = self.fresh_seconds == 0 and self.stale_seconds == 0
        if disabled:
            return
        if self.fresh_seconds < 1:
            raise ValueError("fresh_seconds must be positive when caching is enabled")
        if self.stale_seconds < self.fresh_seconds:
            raise ValueError("stale_seconds must be at least fresh_seconds")
        if self.lock_seconds < 1:
            raise ValueError("lock_seconds must be positive")

    @property
    def enabled(self) -> bool:
        return self.fresh_seconds > 0 and self.stale_seconds > 0


@dataclass(frozen=True, slots=True)
class CacheResult:
    """A payload plus non-sensitive freshness metadata for the HTTP response."""

    payload: dict[str, Any]
    status: CacheStatus
    stored_at: float
    age_seconds: int

    @property
    def freshness(self) -> str:
        return "stale" if self.status == "stale" else "fresh"


def intelligence_cache_key(
    request: Any,
    *,
    namespace: str,
    principal: RolePrincipal,
    scope: Mapping[str, Any],
    query: Mapping[str, Any],
) -> str:
    """Hash an exact authorization and request identity into a private key.

    The bridge ``user_id`` alone is insufficient: one user may own several role
    accounts.  Permission scopes are included in addition to the selected
    resource scope so two principals can never borrow a cached authorization
    projection after a membership or canonical account-type change.
    """

    permissions, permission_scopes = get_effective_permission_context(request)
    identity = {
        "version": 3,
        "tenant": current_schema(),
        "principal": [principal.user_id, principal.kind, principal.principal_id],
        "effective_permissions": list(permissions),
        "effective_permission_scopes": [
            [row.branch_id, row.department_id, list(row.permissions)] for row in permission_scopes
        ],
        "scope": scope,
        "query": query,
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"intelligence:{namespace}:v3:{stable_hash(encoded)}"


def get_or_compute(
    *,
    backend: CacheBackend,
    key: str,
    policy: CachePolicy,
    loader: Callable[[], dict[str, Any]],
    logger: logging.Logger,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    cold_wait_seconds: float = 1.0,
    poll_interval_seconds: float = 0.05,
) -> CacheResult:
    """Return a fresh/stale entry or compute an authoritative payload.

    A stale request that wins ``cache.add`` refreshes synchronously; concurrent
    losers return the stale payload immediately.  A cold loser polls briefly in
    case the winner fills the cache, then preserves the historical cold-load
    contract by running the SQL loader itself.  The lease is intentionally left
    to its bounded TTL, avoiding an unsafe get/delete race with a later owner.
    """

    now = clock()
    if not policy.enabled:
        return _computed_result(loader(), status="disabled", stored_at=now)

    try:
        cached = _decode_entry(
            backend.get(key),
            now=now,
            stale_seconds=policy.stale_seconds,
        )
    except Exception:
        logger.warning("Intelligence cache read failed.", exc_info=True)
        return _computed_result(loader(), status="bypass", stored_at=clock())

    if cached is not None and cached.age_seconds <= policy.fresh_seconds:
        return CacheResult(
            payload=cached.payload,
            status="hit",
            stored_at=cached.stored_at,
            age_seconds=cached.age_seconds,
        )

    try:
        lock_acquired = bool(
            backend.add(
                f"{key}:refresh-lock:v1",
                1,
                timeout=policy.lock_seconds,
            )
        )
    except Exception:
        logger.warning("Intelligence cache refresh lock failed.", exc_info=True)
        return _compute_and_store(
            backend=backend,
            key=key,
            policy=policy,
            loader=loader,
            logger=logger,
            status="bypass",
            clock=clock,
        )

    if not lock_acquired:
        if cached is not None:
            return CacheResult(
                payload=cached.payload,
                status="stale",
                stored_at=cached.stored_at,
                age_seconds=cached.age_seconds,
            )
        # A cold miss has no safe stale value. Give the winner a short bounded
        # opportunity to populate Redis before falling back to our own SQL load.
        wait_for = max(0.0, min(cold_wait_seconds, float(policy.lock_seconds)))
        interval = max(0.01, min(poll_interval_seconds, wait_for or 0.01))
        deadline = monotonic() + wait_for
        polls_remaining = max(1, int(wait_for / interval) + 2)
        while polls_remaining > 0:
            polls_remaining -= 1
            try:
                raced = _decode_entry(
                    backend.get(key),
                    now=clock(),
                    stale_seconds=policy.stale_seconds,
                )
            except Exception:
                logger.warning("Intelligence cache race read failed.", exc_info=True)
                raced = None
                break
            if raced is not None:
                return CacheResult(
                    payload=raced.payload,
                    status=("hit" if raced.age_seconds <= policy.fresh_seconds else "stale"),
                    stored_at=raced.stored_at,
                    age_seconds=raced.age_seconds,
                )
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleeper(min(interval, remaining))

    return _compute_and_store(
        backend=backend,
        key=key,
        policy=policy,
        loader=loader,
        logger=logger,
        status="refresh" if cached is not None else "miss",
        clock=clock,
    )


def apply_cache_headers(response: HttpResponse, result: CacheResult) -> None:
    """Expose freshness without revealing the private cache identity."""

    response["X-Cache-Status"] = result.status
    response["X-Data-Freshness"] = result.freshness
    response["X-Data-Age-Seconds"] = str(result.age_seconds)
    generated_at = result.payload.get("generated_at")
    if isinstance(generated_at, str) and generated_at:
        response["X-Data-Generated-At"] = generated_at
    else:
        response["X-Data-Generated-At"] = datetime.fromtimestamp(
            result.stored_at,
            tz=UTC,
        ).isoformat()


def _compute_and_store(
    *,
    backend: CacheBackend,
    key: str,
    policy: CachePolicy,
    loader: Callable[[], dict[str, Any]],
    logger: logging.Logger,
    status: CacheStatus,
    clock: Callable[[], float],
) -> CacheResult:
    payload = loader()
    stored_at = clock()
    try:
        backend.set(
            key,
            {
                "version": _ENTRY_VERSION,
                "stored_at": stored_at,
                "payload": payload,
            },
            timeout=policy.stale_seconds,
        )
    except Exception:
        logger.warning("Intelligence cache write failed.", exc_info=True)
        status = "bypass"
    return _computed_result(payload, status=status, stored_at=stored_at)


def _computed_result(
    payload: dict[str, Any],
    *,
    status: CacheStatus,
    stored_at: float,
) -> CacheResult:
    return CacheResult(
        payload=payload,
        status=status,
        stored_at=stored_at,
        age_seconds=0,
    )


def _decode_entry(
    value: Any,
    *,
    now: float,
    stale_seconds: int,
) -> CacheResult | None:
    if not isinstance(value, dict) or value.get("version") != _ENTRY_VERSION:
        return None
    stored_at = value.get("stored_at")
    payload = value.get("payload")
    if (
        isinstance(stored_at, bool)
        or not isinstance(stored_at, (int, float))
        or not isinstance(payload, dict)
    ):
        return None
    age = now - float(stored_at)
    if age < -_FUTURE_CLOCK_TOLERANCE_SECONDS or age > stale_seconds:
        return None
    return CacheResult(
        payload=payload,
        status="hit",
        stored_at=float(stored_at),
        age_seconds=max(0, int(age)),
    )
