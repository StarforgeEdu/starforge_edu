"""Payme (Paycom) JSON-RPC Merchant API client (D3-B-3).

Payme speaks JSON-RPC 2.0 over HTTP. Every response — success OR error — is
HTTP 200; errors live in the ``error`` member (this is the TD-18 envelope
exception documented in WORKLOG, the Payme protocol is non-negotiable).

Spec compliance (DAY-3.md D3-B-3, exact):
- HTTP Basic auth ``Paycom:<key>`` — wrong/absent → error ``-32504``.
- Amounts are in **tiyin** (``int(total_uzs * 100)``); a mismatch → ``-31001``.
- ``account`` object is passed through and echoed; an unknown invoice / bad
  account field → an error in **-31050..-31099** with a ``data`` member naming
  the offending field.
- Transaction states: ``1`` created, ``2`` performed, ``-1`` cancelled (while
  created), ``-2`` cancelled (after performed). All times in **milliseconds**.
- Unknown method → ``-32601``.
- ``CreateTransaction`` is idempotent on the Payme ``id``; a second, different
  transaction for the same still-open account → ``-31099``.

The client is transport-agnostic: it parses + validates and delegates DB
transitions to a small ``store`` object (``apps.payments.services`` provides it),
so it is unit-testable with a fake store and Lane F can assert exact codes.

Pattern: ABC + real + mock + settings factory (CODE-GUIDE §6).
`[OWNER:O-4]`. The merchant key is the only credential — verified, never stored
in plaintext (it lives in ``ProviderConfig.payme_key`` via EncryptedCharField).
"""

from __future__ import annotations

import base64
import hmac
from abc import ABC, abstractmethod
from typing import Any, Protocol

from django.conf import settings

# --- JSON-RPC / Payme error codes ------------------------------------------
ERR_INVALID_AMOUNT = -31001
ERR_TRANSACTION_NOT_FOUND = -31003
ERR_CANNOT_PERFORM = -31008
ERR_CANNOT_CANCEL = -31007
ERR_ACCOUNT_NOT_FOUND = -31050  # base of the -31050..-31099 account-error band
ERR_ACCOUNT_ALREADY_PAID = -31099  # another open/performed txn for this account
ERR_INSUFFICIENT_PRIVILEGE = -32504
ERR_METHOD_NOT_FOUND = -32601
ERR_PARSE = -32700

# --- Payme transaction states ----------------------------------------------
STATE_CREATED = 1
STATE_PERFORMED = 2
STATE_CANCELLED = -1  # cancelled while in CREATED
STATE_CANCELLED_AFTER_PERFORM = -2  # cancelled after PERFORMED (refund)


class PaymeError(Exception):
    """A JSON-RPC error. ``data`` names the offending field for account errors."""

    def __init__(self, code: int, message: str | dict[str, str], *, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)

    def as_rpc(self, rpc_id: Any) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


# Localized-message triplets Payme expects (uz/ru/en).
def _msg(en: str, ru: str = "", uz: str = "") -> dict[str, str]:
    return {"ru": ru or en, "uz": uz or en, "en": en}


class PaymeStore(Protocol):
    """The DB-facing side the client delegates to. Implemented in services.py."""

    def find_account(
        self, account: dict[str, Any]
    ) -> Any: ...  # returns an invoice-like or raises PaymeError
    def expected_amount_tiyin(self, invoice: Any) -> int: ...
    def get_transaction(self, payme_id: str) -> Any | None: ...
    def create_transaction(
        self, *, payme_id: str, time_ms: int, amount_tiyin: int, account: dict, invoice: Any
    ) -> Any: ...
    def perform_transaction(self, txn: Any) -> Any: ...
    def cancel_transaction(self, txn: Any, *, reason: int) -> Any: ...
    def statement(self, *, frm: int, to: int) -> list[dict[str, Any]]: ...


class PaymeClient(ABC):
    PROVIDER = "payme"

    # ----- auth ------------------------------------------------------------
    def verify_auth(self, *, auth_header: str | None, key: str) -> bool:
        """Validate the ``Authorization: Basic base64(Paycom:<key>)`` header."""
        if not auth_header or not auth_header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(auth_header.split(" ", 1)[1]).decode()
        except (ValueError, UnicodeDecodeError):
            return False
        login, _, token = raw.partition(":")
        # Constant-time compare on the secret to avoid leaking the key byte-by-byte
        # via response-timing (the login name is not secret, so a plain == is fine).
        return login == "Paycom" and bool(key) and hmac.compare_digest(token, key)

    @abstractmethod
    def handle(
        self, *, body: dict[str, Any], auth_header: str | None, key: str, store: PaymeStore
    ) -> dict[str, Any]:
        """Dispatch a JSON-RPC request → a JSON-RPC response dict (HTTP 200 always)."""

    @abstractmethod
    def build_checkout(
        self, *, amount_tiyin: int, account: dict[str, Any], config: Any
    ) -> dict[str, Any]: ...


class RealPaymeClient(PaymeClient):
    def handle(
        self, *, body: dict[str, Any], auth_header: str | None, key: str, store: PaymeStore
    ) -> dict[str, Any]:
        rpc_id = body.get("id")
        if not self.verify_auth(auth_header=auth_header, key=key):
            return PaymeError(
                ERR_INSUFFICIENT_PRIVILEGE,
                _msg("Insufficient privilege to perform this method."),
            ).as_rpc(rpc_id)

        method = str(body.get("method") or "")
        # Attacker-controlled body: a non-dict `params` ([...], "x", 5) would make the
        # handlers' params.get(...) / params[...] raise below. Coerce so a malformed
        # request becomes a JSON-RPC error, never a 500 (Payme's always-200 contract).
        raw_params = body.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        handler = {
            "CheckPerformTransaction": self._check_perform,
            "CreateTransaction": self._create,
            "PerformTransaction": self._perform,
            "CancelTransaction": self._cancel,
            "CheckTransaction": self._check,
            "GetStatement": self._statement,
        }.get(method)
        if handler is None:
            return PaymeError(ERR_METHOD_NOT_FOUND, _msg("Method not found.")).as_rpc(rpc_id)

        try:
            result = handler(params, store)
        except PaymeError as exc:
            return exc.as_rpc(rpc_id)
        except (KeyError, TypeError, ValueError):
            # A required param is missing or the wrong type (e.g. CreateTransaction
            # without an "id"/"amount"). Malformed input, not a server fault — answer
            # with the JSON-RPC parse error rather than letting it become a 500.
            return PaymeError(ERR_PARSE, _msg("Invalid parameters.")).as_rpc(rpc_id)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    # ----- methods ---------------------------------------------------------
    def _check_perform(self, params: dict[str, Any], store: PaymeStore) -> dict[str, Any]:
        invoice = store.find_account(params.get("account") or {})
        self._assert_amount(params, store, invoice)
        return {"allow": True}

    def _create(self, params: dict[str, Any], store: PaymeStore) -> dict[str, Any]:
        payme_id = str(params["id"])
        existing = store.get_transaction(payme_id)
        if existing is not None:
            # Idempotent on Payme id — echo the existing transaction.
            return self._txn_create_result(existing)

        invoice = store.find_account(params.get("account") or {})
        self._assert_amount(params, store, invoice)
        txn = store.create_transaction(
            payme_id=payme_id,
            time_ms=int(params.get("time", 0)),
            amount_tiyin=int(params["amount"]),
            account=params.get("account") or {},
            invoice=invoice,
        )
        return self._txn_create_result(txn)

    def _perform(self, params: dict[str, Any], store: PaymeStore) -> dict[str, Any]:
        txn = self._require_txn(params, store)
        if txn.provider_state == STATE_PERFORMED:
            return {
                "transaction": txn.provider_txn_id,
                "perform_time": txn.perform_time_ms,
                "state": STATE_PERFORMED,
            }
        if txn.provider_state != STATE_CREATED:
            raise PaymeError(ERR_CANNOT_PERFORM, _msg("Cannot perform transaction."))
        txn = store.perform_transaction(txn)
        return {
            "transaction": txn.provider_txn_id,
            "perform_time": txn.perform_time_ms,
            "state": STATE_PERFORMED,
        }

    def _cancel(self, params: dict[str, Any], store: PaymeStore) -> dict[str, Any]:
        txn = self._require_txn(params, store)
        if txn.provider_state in (STATE_CANCELLED, STATE_CANCELLED_AFTER_PERFORM):
            return {
                "transaction": txn.provider_txn_id,
                "cancel_time": txn.cancel_time_ms,
                "state": txn.provider_state,
            }
        txn = store.cancel_transaction(txn, reason=int(params.get("reason", 0)))
        return {
            "transaction": txn.provider_txn_id,
            "cancel_time": txn.cancel_time_ms,
            "state": txn.provider_state,
        }

    def _check(self, params: dict[str, Any], store: PaymeStore) -> dict[str, Any]:
        txn = self._require_txn(params, store)
        return {
            "create_time": txn.create_time_ms,
            "perform_time": txn.perform_time_ms or 0,
            "cancel_time": txn.cancel_time_ms or 0,
            "transaction": txn.provider_txn_id,
            "state": txn.provider_state,
            "reason": txn.cancel_reason,
        }

    def _statement(self, params: dict[str, Any], store: PaymeStore) -> dict[str, Any]:
        return {"transactions": store.statement(frm=int(params.get("from", 0)), to=int(params.get("to", 0)))}

    # ----- helpers ---------------------------------------------------------
    def _assert_amount(self, params: dict[str, Any], store: PaymeStore, invoice: Any) -> None:
        expected = store.expected_amount_tiyin(invoice)
        if int(params.get("amount", -1)) != expected:
            raise PaymeError(ERR_INVALID_AMOUNT, _msg("Incorrect amount."))

    def _require_txn(self, params: dict[str, Any], store: PaymeStore) -> Any:
        txn = store.get_transaction(str(params.get("id")))
        if txn is None:
            raise PaymeError(ERR_TRANSACTION_NOT_FOUND, _msg("Transaction not found."))
        return txn

    @staticmethod
    def _txn_create_result(txn: Any) -> dict[str, Any]:
        return {
            "create_time": txn.create_time_ms,
            "transaction": txn.provider_txn_id,
            "state": txn.provider_state,
        }

    def build_checkout(self, *, amount_tiyin: int, account: dict[str, Any], config: Any) -> dict[str, Any]:
        merchant = getattr(config, "payme_merchant_id", "")
        acct = ";".join(f"ac.{k}={v}" for k, v in account.items())
        raw = f"m={merchant};{acct};a={amount_tiyin}"
        token = base64.b64encode(raw.encode()).decode()
        return {"redirect_url": f"{settings.PAYME_CHECKOUT_URL}/{token}", "rpc_payload": None}


class MockPaymeClient(RealPaymeClient):
    """The mock reuses the REAL dispatch + spec logic (so error codes / tiyin
    math / state machine are exercised identically) — only the outbound checkout
    URL is canned. Determinism comes from the store implementation."""

    def build_checkout(self, *, amount_tiyin: int, account: dict[str, Any], config: Any) -> dict[str, Any]:
        acct = ";".join(f"{k}={v}" for k, v in account.items())
        return {
            "redirect_url": f"mock://payme/checkout?{acct}&amount={amount_tiyin}",
            "rpc_payload": None,
            "mock": True,
        }


def get_payme_client() -> PaymeClient:
    if getattr(settings, "PAYME_USE_MOCK", True):
        return MockPaymeClient()
    return RealPaymeClient()
