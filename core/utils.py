"""Small utility helpers used across apps."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any, Protocol

from django.conf import settings
from django.db import connection


class AnyRequest(Protocol):
    """Structural request type shared by Django and DRF request objects."""

    META: dict[str, Any]


def current_schema() -> str:
    """The active django-tenants schema name (one typed access point for it)."""
    return connection.schema_name  # type: ignore[attr-defined]


def client_ip(request: AnyRequest) -> str:
    """Client IP with rightmost-trusted-hop semantics.

    ``X-Forwarded-For`` is honored only for the configured ``NUM_PROXIES``
    trusted hops, counted from the right (each trusted proxy appends exactly
    one address). With the default of 0 only ``REMOTE_ADDR`` is trusted, so a
    client-supplied header can never spoof the IP used by the OTP per-IP cap
    or audit logs.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "") or ""
    num_proxies = int(getattr(settings, "NUM_PROXIES", 0) or 0)
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if num_proxies > 0 and forwarded:
        addrs = forwarded.split(",")
        return addrs[-min(num_proxies, len(addrs))].strip()
    return remote_addr


def user_agent(request: AnyRequest) -> str:
    return request.META.get("HTTP_USER_AGENT", "")[:512]


def generate_otp(length: int = 6) -> str:
    """Cryptographically random numeric OTP."""

    upper = 10**length
    return f"{secrets.randbelow(upper):0{length}d}"


def stable_hash(value: str) -> str:
    """SHA-256 hash, hex. Used for prompt-cache keys and idempotency keys."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
