"""Soliq fiscalization client (TD-7, D3-B-9).

Soliq (Uzbekistan's State Tax Committee) requires every completed payment to be
fiscalized: the merchant submits the receipt, Soliq returns a ``fiscal_sign``
and a QR-code URL the customer can scan to verify it with the tax authority.

Pattern: ABC + real client + mock + settings factory (CODE-GUIDE §6). The real
client imports its heavy/native transport LAZILY inside the method that needs it
(mirrors infrastructure/ai/anthropic_client and the academics transcript task) so
the app loads where the dependency is absent. ``requests`` IS available, so the
real client uses it; nothing here imports an uninstalled lib at module level.

`[OWNER:O-5]` — production needs Soliq merchant credentials + the GNK endpoint.
``SOLIQ_USE_MOCK`` defaults True; the mock returns a DETERMINISTIC fiscal sign +
QR url derived from the idempotency key, so Lane F can predict signatures.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import Any

from django.conf import settings


class FiscalClient(ABC):
    @abstractmethod
    def fiscalize(
        self, *, payment_id: int, amount_uzs: str, items: list[dict[str, Any]], idempotency_key: str
    ) -> dict[str, Any]:
        """Submit a receipt; return ``{"fiscal_sign", "qr_url", "raw"}``.

        ``amount_uzs`` is a decimal string (never float — exact money). The
        caller persists the marker on the source row so a retried task no-ops.
        """


class MockSoliqClient(FiscalClient):
    """Deterministic mock (``SOLIQ_USE_MOCK=True``). The fiscal sign is a stable
    hash of the idempotency key so the same payment always fiscalizes to the same
    sign (idempotency + Lane F predictability)."""

    def fiscalize(
        self, *, payment_id: int, amount_uzs: str, items: list[dict[str, Any]], idempotency_key: str
    ) -> dict[str, Any]:
        sign = hashlib.sha256(f"soliq:{idempotency_key}".encode()).hexdigest()[:64]
        qr_base = getattr(settings, "SOLIQ_QR_BASE_URL", "https://ofd.soliq.uz/check")
        return {
            "fiscal_sign": sign,
            "qr_url": f"{qr_base}?c={sign}&amount={amount_uzs}",
            "raw": {"mock": True, "payment_id": payment_id, "items": items},
        }


class SoliqClient(FiscalClient):
    """Real GNK/Soliq client. Uses ``requests`` (available) with a timeout on
    every call (CODE-GUIDE §6); never call from a request handler — Celery only."""

    def __init__(self, *, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def fiscalize(
        self, *, payment_id: int, amount_uzs: str, items: list[dict[str, Any]], idempotency_key: str
    ) -> dict[str, Any]:
        import requests  # available; kept local to mirror the lazy-transport pattern

        resp = requests.post(
            f"{self.base_url}/v1/receipt",
            json={"amount": amount_uzs, "items": items, "external_id": idempotency_key},
            headers={"Authorization": f"Bearer {self.token}", "Idempotency-Key": idempotency_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"fiscal_sign": data["fiscal_sign"], "qr_url": data["qr_url"], "raw": data}


def get_fiscal_client() -> FiscalClient:
    if getattr(settings, "SOLIQ_USE_MOCK", True):
        return MockSoliqClient()
    return SoliqClient(
        base_url=getattr(settings, "SOLIQ_API_URL", ""),
        token=getattr(settings, "SOLIQ_API_TOKEN", ""),
    )
