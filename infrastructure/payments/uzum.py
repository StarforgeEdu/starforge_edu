"""Legacy Uzum callback compatibility client.

This HMAC shape originated in the project's mock-era contract; it is not Uzum
Bank's current documented Merchant API (Basic auth with separate check/create/
confirm/reverse/status operations). Production settings disable the route and
checkout. Keeping the isolated implementation lets old fixtures remain useful
without misrepresenting it as a deployable provider integration.

Pattern: ABC + real + mock + settings factory (CODE-GUIDE §6). The mock uses the
real HMAC algorithm so tampering tests are meaningful. `[OWNER:O-6]` — mock-first
(TD-2). The merchant key lives in ``ProviderConfig.uzum_api_key`` (encrypted).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from abc import ABC, abstractmethod
from typing import Any

from django.conf import settings

_HEX_SHA256 = re.compile(r"\A[0-9a-fA-F]{64}\Z")
_SAFE_PROVIDER_VALUE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def uzum_signature(*, payload: dict[str, Any], api_key: str) -> str:
    """HMAC-SHA256 hex digest over the canonical (sorted, compact) JSON body.

    The signature itself is never part of the signed body — it travels in the
    ``X-Signature`` header. Consequently every body key is authenticated; ignoring
    a body field would create a malleability gap where it could be changed without
    invalidating the signature.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hmac.new(api_key.encode(), body.encode(), hashlib.sha256).hexdigest()


class UzumClient(ABC):
    PROVIDER = "uzum"

    @abstractmethod
    def verify_signature(self, *, payload: dict[str, Any], signature: str, api_key: str) -> bool: ...

    @abstractmethod
    def build_checkout(self, *, amount_uzs: int, order_id: str, config: Any) -> dict[str, Any]: ...


class RealUzumClient(UzumClient):
    def verify_signature(self, *, payload: dict[str, Any], signature: str, api_key: str) -> bool:
        if not signature or not api_key or _HEX_SHA256.fullmatch(signature) is None:
            return False
        try:
            expected = uzum_signature(payload=payload, api_key=api_key)
        except (TypeError, UnicodeError, ValueError):
            return False
        return hmac.compare_digest(expected.lower(), signature.lower())

    def build_checkout(self, *, amount_uzs: int, order_id: str, config: Any) -> dict[str, Any]:
        from urllib.parse import urlencode

        from infrastructure.http_client import validate_https_endpoint

        merchant = str(getattr(config, "uzum_merchant_id", ""))
        if (
            _SAFE_PROVIDER_VALUE.fullmatch(merchant) is None
            or _SAFE_PROVIDER_VALUE.fullmatch(str(order_id)) is None
        ):
            raise ValueError("Legacy Uzum checkout configuration is invalid.")
        base = validate_https_endpoint(
            settings.UZUM_CHECKOUT_URL,
            allowed_hosts=getattr(settings, "UZUM_CHECKOUT_ALLOWED_HOSTS", ()),
        ).rstrip("/")
        url = f"{base}?{urlencode({'merchant': merchant, 'order': order_id, 'amount': amount_uzs})}"
        return {"redirect_url": url, "order_id": order_id}


class MockUzumClient(UzumClient):
    def verify_signature(self, *, payload: dict[str, Any], signature: str, api_key: str) -> bool:
        return RealUzumClient().verify_signature(payload=payload, signature=signature, api_key=api_key)

    def build_checkout(self, *, amount_uzs: int, order_id: str, config: Any) -> dict[str, Any]:
        return {
            "redirect_url": f"mock://uzum/checkout/{order_id}?amount={amount_uzs}",
            "order_id": order_id,
            "mock": True,
        }


def get_uzum_client() -> UzumClient:
    if getattr(settings, "UZUM_USE_MOCK", True):
        return MockUzumClient()
    return RealUzumClient()
