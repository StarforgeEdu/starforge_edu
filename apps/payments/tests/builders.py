"""Provider webhook payload **builders** (TESTING.md §8).

Lane F (D3-F) writes attack/golden tests against these helpers instead of
copy-pasting raw dicts. Valid, tampered, and replayed variants are produced by
flags so a single source describes the wire shape each provider speaks.

Mirrors the DAY-3.md Lane B contract:

- **Payme** speaks JSON-RPC 2.0 over HTTP (always HTTP 200; errors in the
  ``error`` member). Auth is HTTP Basic ``Paycom:<key>``. Amounts are in
  **tiyin** (``int(total_uzs * 100)``). All times are epoch **milliseconds**.
  Methods: CheckPerformTransaction, CreateTransaction, PerformTransaction,
  CancelTransaction, CheckTransaction, GetStatement.
- **Click** signs with ``md5(click_trans_id + service_id + SECRET_KEY +
  merchant_trans_id [+ merchant_prepare_id on complete] + amount + action +
  sign_time)``. action 0 = prepare, action 1 = complete.
- **Uzum** signs the canonical JSON body with an HMAC keyed on the api key.

These builders are pure-Python (no provider SDK import) and deterministic so the
exact signature can be predicted in a test (Lane B publishes the mock
determinism rules in WORKLOG; the math here matches the spec strings verbatim).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

# Deterministic credentials used across the Lane-F suite. A real test seeds the
# tenant's ProviderConfig with these so the builder's signatures verify.
PAYME_KEY = "payme_test_secret_key"
CLICK_SECRET_KEY = "click_test_secret_key"
CLICK_SERVICE_ID = "12345"
CLICK_MERCHANT_ID = "67890"
UZUM_API_KEY = "uzum_test_secret_key"

# Payme JSON-RPC error codes (DAY-3.md D3-B-3 / D3-F-10).
PAYME_ERR_AUTH = -32504  # HTTP Basic failed
PAYME_ERR_METHOD = -32601  # unknown JSON-RPC method
PAYME_ERR_AMOUNT = -31001  # amount mismatch (tiyin)
PAYME_ERR_UNKNOWN_TXN = -31003  # transaction id not found
PAYME_ERR_CANNOT_PERFORM = -31008  # state does not allow the transition
PAYME_ERR_ACCOUNT_LOW = -31050  # account field errors live in -31050..-31099
PAYME_ERR_ACCOUNT_HIGH = -31099
PAYME_ERR_ALREADY_EXISTS = -31099  # second concurrent txn for the same account

# Payme transaction states (DAY-3.md): 1 created, 2 performed, -1/-2 cancelled.
PAYME_STATE_CREATED = 1
PAYME_STATE_PERFORMED = 2
PAYME_STATE_CANCELLED_BEFORE = -1
PAYME_STATE_CANCELLED_AFTER = -2


# --------------------------------------------------------------------------- #
# Payme
# --------------------------------------------------------------------------- #
def payme_basic_auth(*, key: str = PAYME_KEY, login: str = "Paycom") -> str:
    """HTTP Basic header value: ``Basic base64("Paycom:<key>")``."""
    raw = f"{login}:{key}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def payme_auth_headers(*, key: str = PAYME_KEY, wrong: bool = False) -> dict[str, str]:
    """Header dict for a Django test client POST.

    ``wrong=True`` flips the key so the merchant rejects auth with -32504.
    """
    if wrong:
        key = key + "_TAMPERED"
    return {"HTTP_AUTHORIZATION": payme_basic_auth(key=key)}


def tiyin(amount_uzs: float | str | int) -> int:
    """UZS -> tiyin, the integer unit Payme transmits (1 UZS = 100 tiyin)."""
    from decimal import Decimal

    return int(Decimal(str(amount_uzs)) * 100)


def make_payme_rpc(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    rpc_id: int = 1,
) -> dict[str, Any]:
    """A JSON-RPC 2.0 request envelope."""
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}


def payme_check_perform(*, amount_tiyin: int, account: dict[str, Any], rpc_id: int = 1) -> dict[str, Any]:
    return make_payme_rpc(
        "CheckPerformTransaction",
        {"amount": amount_tiyin, "account": account},
        rpc_id=rpc_id,
    )


def payme_create_transaction(
    *,
    payme_id: str,
    amount_tiyin: int,
    account: dict[str, Any],
    time_ms: int = 1_700_000_000_000,
    rpc_id: int = 1,
) -> dict[str, Any]:
    return make_payme_rpc(
        "CreateTransaction",
        {"id": payme_id, "time": time_ms, "amount": amount_tiyin, "account": account},
        rpc_id=rpc_id,
    )


def payme_perform_transaction(*, payme_id: str, rpc_id: int = 1) -> dict[str, Any]:
    return make_payme_rpc("PerformTransaction", {"id": payme_id}, rpc_id=rpc_id)


def payme_cancel_transaction(*, payme_id: str, reason: int = 3, rpc_id: int = 1) -> dict[str, Any]:
    return make_payme_rpc("CancelTransaction", {"id": payme_id, "reason": reason}, rpc_id=rpc_id)


def payme_check_transaction(*, payme_id: str, rpc_id: int = 1) -> dict[str, Any]:
    return make_payme_rpc("CheckTransaction", {"id": payme_id}, rpc_id=rpc_id)


def payme_get_statement(*, from_ms: int, to_ms: int, rpc_id: int = 1) -> dict[str, Any]:
    return make_payme_rpc("GetStatement", {"from": from_ms, "to": to_ms}, rpc_id=rpc_id)


# --------------------------------------------------------------------------- #
# Click
# --------------------------------------------------------------------------- #
def click_sign(
    *,
    click_trans_id: str,
    service_id: str,
    secret_key: str,
    merchant_trans_id: str,
    amount: str,
    action: int,
    sign_time: str,
    merchant_prepare_id: str | None = None,
) -> str:
    """md5 sign string per DAY-3.md D3-B-2.

    prepare (action=0):
      md5(click_trans_id + service_id + SECRET_KEY + merchant_trans_id +
          amount + action + sign_time)
    complete (action=1): same, with merchant_prepare_id inserted after
      merchant_trans_id.
    """
    parts = [click_trans_id, service_id, secret_key, merchant_trans_id]
    if merchant_prepare_id is not None:
        parts.append(merchant_prepare_id)
    parts += [amount, str(action), sign_time]
    return hashlib.md5("".join(parts).encode()).hexdigest()


def make_click_prepare(
    *,
    click_trans_id: str = "1001",
    merchant_trans_id: str = "INV-2026-000001",
    amount: str = "150000.00",
    sign_time: str = "2026-06-16 09:00:00",
    service_id: str = CLICK_SERVICE_ID,
    secret_key: str = CLICK_SECRET_KEY,
    tamper_sign: bool = False,
) -> dict[str, Any]:
    action = 0
    sign = click_sign(
        click_trans_id=click_trans_id,
        service_id=service_id,
        secret_key=secret_key,
        merchant_trans_id=merchant_trans_id,
        amount=amount,
        action=action,
        sign_time=sign_time,
    )
    if tamper_sign:
        sign = _flip_one_char(sign)
    return {
        "click_trans_id": click_trans_id,
        "service_id": service_id,
        "merchant_trans_id": merchant_trans_id,
        "amount": amount,
        "action": action,
        "sign_time": sign_time,
        "sign_string": sign,
    }


def make_click_complete(
    *,
    click_trans_id: str = "1001",
    merchant_trans_id: str = "INV-2026-000001",
    merchant_prepare_id: str = "1",
    amount: str = "150000.00",
    sign_time: str = "2026-06-16 09:01:00",
    service_id: str = CLICK_SERVICE_ID,
    secret_key: str = CLICK_SECRET_KEY,
    tamper_sign: bool = False,
) -> dict[str, Any]:
    action = 1
    sign = click_sign(
        click_trans_id=click_trans_id,
        service_id=service_id,
        secret_key=secret_key,
        merchant_trans_id=merchant_trans_id,
        merchant_prepare_id=merchant_prepare_id,
        amount=amount,
        action=action,
        sign_time=sign_time,
    )
    if tamper_sign:
        sign = _flip_one_char(sign)
    return {
        "click_trans_id": click_trans_id,
        "service_id": service_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_prepare_id": merchant_prepare_id,
        "amount": amount,
        "action": action,
        "sign_time": sign_time,
        "sign_string": sign,
    }


# --------------------------------------------------------------------------- #
# Uzum
# --------------------------------------------------------------------------- #
def uzum_sign(*, body: dict[str, Any], api_key: str = UZUM_API_KEY) -> str:
    """HMAC-SHA256 over the canonical (sorted, compact) JSON body."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(api_key.encode(), canonical, hashlib.sha256).hexdigest()


def make_uzum_webhook(
    *,
    event_id: str = "uzum-evt-1",
    order_id: str = "INV-2026-000001",
    amount: str = "150000.00",
    status: str = "PAID",
    api_key: str = UZUM_API_KEY,
    tamper_sign: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Returns ``(body, headers)``. ``tamper_sign`` corrupts the HMAC."""
    body = {"event_id": event_id, "order_id": order_id, "amount": amount, "status": status}
    sig = uzum_sign(body=body, api_key=api_key)
    if tamper_sign:
        sig = _flip_one_char(sig)
    return body, {"HTTP_X_SIGNATURE": sig}


def _flip_one_char(s: str) -> str:
    """Flip exactly one hex char so the signature differs but stays well-formed."""
    if not s:
        return "0"
    last = s[-1]
    return s[:-1] + ("0" if last != "0" else "1")
