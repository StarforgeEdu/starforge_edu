"""Small privacy primitives for logs and operational metadata."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from django.conf import settings


def private_fingerprint(value: Any, *, namespace: str) -> str:
    """Return a stable, deployment-local reference without retaining plaintext.

    HMAC prevents an operator with read-only log access from reversing low-entropy
    values (phone numbers, usernames, or IP addresses) with a lookup table. The
    namespace prevents correlating the same value across unrelated data classes.
    """
    normalized = str(value or "").strip().casefold()
    if not normalized:
        return "-"
    key = str(settings.SECRET_KEY).encode("utf-8")
    payload = f"{namespace}\0{normalized}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()[:20]
