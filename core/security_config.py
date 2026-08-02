"""Fail-fast validation for deployment trust-boundary configuration."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


def validate_exact_https_origins(name: str, origins: Sequence[str]) -> tuple[str, ...]:
    """Validate a browser-origin allowlist without permissive URL-parser edges.

    Production CORS/CSRF entries are trust grants, not navigation URLs. Each
    value must therefore be one exact HTTPS origin: scheme, host, and optional
    explicit port only. Paths, credentials, wildcards, control characters, and
    duplicate spellings are rejected before Django starts.
    """
    if isinstance(origins, (str, bytes)):
        raise ImproperlyConfigured(f"{name} must be a list of exact HTTPS origins.")

    validated: list[str] = []
    seen: set[str] = set()
    for index, origin in enumerate(origins):
        if (
            not isinstance(origin, str)
            or not origin
            or origin != origin.strip()
            or "\\" in origin
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in origin
            )
        ):
            raise ImproperlyConfigured(f"{name}[{index}] is not a valid exact HTTPS origin.")
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ImproperlyConfigured(f"{name}[{index}] contains an invalid port.") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "*" in origin
            or port == 0
            or origin != f"https://{parsed.netloc}"
        ):
            raise ImproperlyConfigured(f"{name}[{index}] must be one exact HTTPS origin.")
        canonical = origin.casefold()
        if canonical in seen:
            raise ImproperlyConfigured(f"{name} contains a duplicate origin.")
        seen.add(canonical)
        validated.append(origin)
    return tuple(validated)
