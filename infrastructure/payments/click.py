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
import re
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlencode

from django.conf import settings

# Click action codes.
ACTION_PREPARE = 0
ACTION_COMPLETE = 1

# Click error codes (subset we emit).
ERROR_SUCCESS = 0
ERROR_SIGN_CHECK_FAILED = -1
ERROR_INVALID_AMOUNT = -2
ERROR_ACTION_NOT_FOUND = -3
ERROR_TRANSACTION_NOT_FOUND = -6
ERROR_ALREADY_PAID = -4
ERROR_USER_NOT_FOUND = -5
ERROR_FAILED_TO_UPDATE_USER = -7
ERROR_IN_REQUEST = -8
ERROR_TRANSACTION_CANCELLED = -9
_SAFE_PROVIDER_VALUE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
_HEX_MD5 = re.compile(r"\A[0-9a-fA-F]{32}\Z")


def _wire_text(payload: dict[str, Any], field: str, *, max_length: int = 64) -> str:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field} must be a scalar")
    text = str(value)
    if (
        not text
        or len(text) > max_length
        or text.strip() != text
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise ValueError(f"{field} is invalid")
    return text


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
    # Click.uz's published wire contract requires this exact MD5 digest; it is
    # not a locally chosen primitive and replacing it would reject every real
    # callback. Empty-secret rejection, constant-time comparison, replay keys,
    # amount binding, and tenant-specific credentials provide the surrounding
    # controls. Remove this exception when the provider offers a stronger
    # signature version.
    return hashlib.md5("".join(parts).encode()).hexdigest()  # nosec B324


class ClickClient(ABC):
    PROVIDER = "click"

    @abstractmethod
    def verify_signature(
        self,
        *,
        payload: dict[str, Any],
        secret_key: str,
        expected_service_id: str | None = None,
    ) -> bool: ...

    @abstractmethod
    def build_checkout(self, *, amount_uzs: int, merchant_trans_id: str, config: Any) -> dict[str, Any]: ...


class RealClickClient(ClickClient):
    """Verifies real Click callbacks. No outbound HTTP for the webhook path —
    Click pushes to us; we only verify the signature it sent."""

    def verify_signature(
        self,
        *,
        payload: dict[str, Any],
        secret_key: str,
        expected_service_id: str | None = None,
    ) -> bool:
        # An empty/unset secret must never verify: the service_id is semi-public
        # (it's in the browser-visible checkout redirect), so without this guard an
        # attacker could forge md5(...''...) and pass compare_digest. Mirrors the
        # `not api_key`/`not key` guards in uzum.py and payme.py.
        if not secret_key:
            return False
        try:
            click_trans_id = _wire_text(payload, "click_trans_id")
            service_id = _wire_text(payload, "service_id")
            merchant_trans_id = _wire_text(payload, "merchant_trans_id")
            amount = _wire_text(payload, "amount", max_length=32)
            sign_time = _wire_text(payload, "sign_time")
            raw_action = payload["action"]
            if isinstance(raw_action, bool) or not isinstance(raw_action, (str, int)):
                return False
            action = int(raw_action)
            if action not in (ACTION_PREPARE, ACTION_COMPLETE) or str(raw_action) not in {"0", "1"}:
                return False
            merchant_prepare_id = ""
            if action == ACTION_COMPLETE:
                merchant_prepare_id = _wire_text(payload, "merchant_prepare_id")
            if expected_service_id is not None and service_id != str(expected_service_id):
                return False
            expected = click_sign_string(
                click_trans_id=click_trans_id,
                service_id=service_id,
                secret_key=secret_key,
                merchant_trans_id=merchant_trans_id,
                amount=amount,
                action=action,
                sign_time=sign_time,
                merchant_prepare_id=merchant_prepare_id,
            )
        except (KeyError, TypeError, ValueError):
            return False
        provided = payload.get("sign_string", "")
        if not isinstance(provided, str) or _HEX_MD5.fullmatch(provided) is None:
            return False
        # constant-time compare to avoid leaking the digest byte-by-byte
        return hmac.compare_digest(expected.lower(), provided.lower())

    def build_checkout(self, *, amount_uzs: int, merchant_trans_id: str, config: Any) -> dict[str, Any]:
        from infrastructure.http_client import validate_https_endpoint

        service_id = str(getattr(config, "click_service_id", ""))
        merchant_id = str(getattr(config, "click_merchant_id", ""))
        if not _SAFE_PROVIDER_VALUE.fullmatch(service_id) or not _SAFE_PROVIDER_VALUE.fullmatch(merchant_id):
            raise ValueError("Click merchant configuration is invalid.")
        base = validate_https_endpoint(
            settings.CLICK_CHECKOUT_URL,
            allowed_hosts=getattr(settings, "CLICK_CHECKOUT_ALLOWED_HOSTS", ()),
        ).rstrip("/")
        query = urlencode(
            {
                "service_id": service_id,
                "merchant_id": merchant_id,
                "amount": amount_uzs,
                "transaction_param": merchant_trans_id,
            }
        )
        url = f"{base}?{query}"
        return {"redirect_url": url, "merchant_trans_id": merchant_trans_id}


class MockClickClient(ClickClient):
    """Deterministic mock. The signature it ACCEPTS is the real md5 algorithm
    (so Lane F's tampering tests are meaningful); the checkout url is canned."""

    def verify_signature(
        self,
        *,
        payload: dict[str, Any],
        secret_key: str,
        expected_service_id: str | None = None,
    ) -> bool:
        # Use the real algorithm — a tampered sign_string must still fail.
        return RealClickClient().verify_signature(
            payload=payload,
            secret_key=secret_key,
            expected_service_id=expected_service_id,
        )

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
