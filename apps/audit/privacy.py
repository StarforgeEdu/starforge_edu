"""Privacy projection for authentication-related audit snapshots."""

from __future__ import annotations

from typing import Any

from core.privacy import private_fingerprint

_AUTH_ACTIONS = frozenset({"login", "login_failed", "otp_request", "otp_verify"})
_PLAINTEXT_IDENTIFIER_KEYS = ("identifier", "username")


def privacy_safe_audit_snapshot(
    *,
    action: str,
    resource_type: str,
    snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Remove reversible auth identifiers from storage and presentation.

    Applying this both before new writes and when presenting old rows keeps a
    rolling deployment safe: legacy records containing ``identifier`` or
    ``username`` cannot leak through the API while the immutable history is
    being reviewed.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    if action not in _AUTH_ACTIONS and resource_type != "auth.OTP":
        return snapshot

    sanitized = dict(snapshot)
    plaintext = None
    for key in _PLAINTEXT_IDENTIFIER_KEYS:
        value = sanitized.pop(key, None)
        if plaintext is None and value not in (None, ""):
            plaintext = value
    if plaintext is not None and "identifier_ref" not in sanitized:
        sanitized["identifier_ref"] = private_fingerprint(
            plaintext,
            namespace="auth-identifier",
        )
    return sanitized
