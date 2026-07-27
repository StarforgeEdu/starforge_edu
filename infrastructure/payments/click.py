"""Click.uz payment provider client (D3-B-2).

Click uses a two-phase webhook: ``Prepare`` (action 0) then ``Complete``
(action 1). Each callback carries an md5 ``sign_string`` the merchant must
verify before touching any row (CODE-GUIDE §11). The sign string is:

    Prepare:  md5(click_trans_id + service_id + SECRET_KEY +
                  merchant_trans_id + amount + action + sign_time)
    Complete: md5(click_trans_id + service_id + SECRET_KEY +
                  merchant_trans_id + merchant_prepare_id + amount +
                  action + sign_time)

Error codes follow Click's spec: ``0`` success, ``-1`` SIGN CHECK FAILED.

Pattern: ABC + real client + mock + settings factory (CODE-GUIDE §6). The mock
is deterministic — ids derive from the input — so Lane F can predict them.
`[OWNER:O-3]` — nothing blocks; the mock is the Day-3 deliverable (TD-2).
"""

from __future__ import annotations

import hashlib
import hmac
from abc import ABC, abstractmethod
from typing import Any

from django.conf import settings

# Click action codes.
ACTION_PREPARE = 0
ACTION_COMPLETE = 1

# Click error codes (subset we emit).
ERROR_SUCCESS = 0
ERROR_SIGN_CHECK_FAILED = -1
ERROR_TRANSACTION_NOT_FOUND = -6
ERROR_ALREADY_PAID = -4
ERROR_TRANSACTION_CANCELLED = -9


def click_sign_string(
    *,
    click_trans_id: str,
    service_id: str,
    secret_key: str,
    merchant_trans_id: str,
    amount: str,
    action: int,
    sign_time: str,
    merchant_prepare_id: str = "",
) -> str:
    """The md5 hex digest Click expects in ``sign_string``.

    ``merchant_prepare_id`` is concatenated only for the Complete callback
    (action 1) per Click's spec; for Prepare it is the empty string.
    """
    parts = [click_trans_id, service_id, secret_key, merchant_trans_id]
    if action == ACTION_COMPLETE:
        parts.append(str(merchant_prepare_id))
    parts.extend([amount, str(action), sign_time])
    return hashlib.md5("".join(parts).encode()).hexdigest()


class ClickClient(ABC):
    PROVIDER = "click"

    @abstractmethod
    def verify_signature(self, *, payload: dict[str, Any], secret_key: str) -> bool: ...

    @abstractmethod
    def build_checkout(self, *, amount_uzs: int, merchant_trans_id: str, config: Any) -> dict[str, Any]: ...


class RealClickClient(ClickClient):
    """Verifies real Click callbacks. No outbound HTTP for the webhook path —
    Click pushes to us; we only verify the signature it sent."""

    def verify_signature(self, *, payload: dict[str, Any], secret_key: str) -> bool:
        # An empty/unset secret must never verify: the service_id is semi-public
        # (it's in the browser-visible checkout redirect), so without this guard an
        # attacker could forge md5(...''...) and pass compare_digest. Mirrors the
        # `not api_key`/`not key` guards in uzum.py and payme.py.
        if not secret_key:
            return False
        try:
            expected = click_sign_string(
                click_trans_id=str(payload["click_trans_id"]),
                service_id=str(payload["service_id"]),
                secret_key=secret_key,
                merchant_trans_id=str(payload["merchant_trans_id"]),
                amount=str(payload["amount"]),
                action=int(payload["action"]),
                sign_time=str(payload["sign_time"]),
                merchant_prepare_id=str(payload.get("merchant_prepare_id", "")),
            )
        except (KeyError, TypeError, ValueError):
            return False
        provided = str(payload.get("sign_string", ""))
        # constant-time compare to avoid leaking the digest byte-by-byte
        return hmac.compare_digest(expected, provided)

    def build_checkout(self, *, amount_uzs: int, merchant_trans_id: str, config: Any) -> dict[str, Any]:
        service_id = getattr(config, "click_service_id", "")
        merchant_id = getattr(config, "click_merchant_id", "")
        url = (
            f"{settings.CLICK_CHECKOUT_URL}?service_id={service_id}"
            f"&merchant_id={merchant_id}&amount={amount_uzs}"
            f"&transaction_param={merchant_trans_id}"
        )
        return {"redirect_url": url, "merchant_trans_id": merchant_trans_id}


class MockClickClient(ClickClient):
    """Deterministic mock. The signature it ACCEPTS is the real md5 algorithm
    (so Lane F's tampering tests are meaningful); the checkout url is canned."""

    def verify_signature(self, *, payload: dict[str, Any], secret_key: str) -> bool:
        # Use the real algorithm — a tampered sign_string must still fail.
        return RealClickClient().verify_signature(payload=payload, secret_key=secret_key)

    def build_checkout(self, *, amount_uzs: int, merchant_trans_id: str, config: Any) -> dict[str, Any]:
        return {
            "redirect_url": f"mock://click/checkout/{merchant_trans_id}?amount={amount_uzs}",
            "merchant_trans_id": merchant_trans_id,
            "mock": True,
        }


def get_click_client() -> ClickClient:
    if getattr(settings, "CLICK_USE_MOCK", True):
        return MockClickClient()
    return RealClickClient()
