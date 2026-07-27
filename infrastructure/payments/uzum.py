"""Uzum Bank payment provider client (D3-B-4).

Uzum signs webhooks with an HMAC-SHA256 over the canonical (sorted) JSON body
keyed by the merchant API key; the digest arrives in an ``X-Signature`` header
(modelled as ``signature`` in the payload for the test builders). Verify before
touching any row (CODE-GUIDE §11).

Pattern: ABC + real + mock + settings factory (CODE-GUIDE §6). The mock uses the
real HMAC algorithm so tampering tests are meaningful. `[OWNER:O-6]` — mock-first
(TD-2). The merchant key lives in ``ProviderConfig.uzum_api_key`` (encrypted).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from typing import Any

from django.conf import settings


def uzum_signature(*, payload: dict[str, Any], api_key: str) -> str:
    """HMAC-SHA256 hex digest over the canonical (sorted, compact) JSON body.

    The signature itself is never part of the signed body — it travels in the
    ``X-Signature`` header. Any ``signature`` key in ``payload`` is excluded so a
    caller can pass either the raw body or a body with the field already set.
    """
    signable = {k: v for k, v in payload.items() if k != "signature"}
    body = json.dumps(signable, sort_keys=True, separators=(",", ":"))
    return hmac.new(api_key.encode(), body.encode(), hashlib.sha256).hexdigest()


class UzumClient(ABC):
    PROVIDER = "uzum"

    @abstractmethod
    def verify_signature(self, *, payload: dict[str, Any], signature: str, api_key: str) -> bool: ...

    @abstractmethod
    def build_checkout(self, *, amount_uzs: int, order_id: str, config: Any) -> dict[str, Any]: ...


class RealUzumClient(UzumClient):
    def verify_signature(self, *, payload: dict[str, Any], signature: str, api_key: str) -> bool:
        if not signature or not api_key:
            return False
        expected = uzum_signature(payload=payload, api_key=api_key)
        return hmac.compare_digest(expected, signature)

    def build_checkout(self, *, amount_uzs: int, order_id: str, config: Any) -> dict[str, Any]:
        merchant = getattr(config, "uzum_merchant_id", "")
        url = f"{settings.UZUM_CHECKOUT_URL}?merchant={merchant}&order={order_id}&amount={amount_uzs}"
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
