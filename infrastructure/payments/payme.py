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
import binascii
import hmac
import re
from abc import ABC, abstractmethod
from typing import Any, Protocol
from urllib.parse import quote

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
ERR_INTERNAL = -32400

# --- Payme transaction states ----------------------------------------------
STATE_CREATED = 1
STATE_PERFORMED = 2
STATE_CANCELLED = -1  # cancelled while in CREATED
STATE_CANCELLED_AFTER_PERFORM = -2  # cancelled after PERFORMED (refund)
_SAFE_CHECKOUT_VALUE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
_MAX_PAYME_ID_LENGTH = 64
_MAX_STATEMENT_WINDOW_MS = 31 * 24 * 60 * 60 * 1000


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
    def validate_existing_create(
        self,
        txn: Any,
        *,
        time_ms: int,
        amount_tiyin: int,
        account: dict[str, Any],
        invoice: Any,
    ) -> None: ...
    def perform_transaction(self, txn: Any) -> Any: ...
    def cancel_transaction(self, txn: Any, *, reason: int) -> Any: ...
    def statement(self, *, frm: int, to: int) -> list[dict[str, Any]]: ...


class PaymeClient(ABC):
    PROVIDER = "payme"

    # ----- auth ------------------------------------------------------------
    def verify_auth(self, *, auth_header: str | None, key: str) -> bool:
        """Validate the ``Authorization: Basic base64(Paycom:<key>)`` header."""
        if not auth_header or len(auth_header) > 8192:
            return False
        try:
            scheme, encoded = auth_header.split(" ", 1)
            if scheme.lower() != "basic" or not encoded or any(char.isspace() for char in encoded):
                return False
            raw = base64.b64decode(encoded, validate=True).decode("utf-8", errors="strict")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        login, _, token = raw.partition(":")
        # Constant-time compare on the secret to avoid leaking the key byte-by-byte
        # via response-timing (the login name is not secret, so a plain == is fine).
        return (
            login == "Paycom"
            and bool(key)
            and hmac.compare_digest(token.encode("utf-8"), key.encode("utf-8"))
        )

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
        raw_rpc_id = body.get("id")
        rpc_id = (
            raw_rpc_id
            if isinstance(raw_rpc_id, int)
            and not isinstance(raw_rpc_id, bool)
            and -(2**63) <= raw_rpc_id <= 2**63 - 1
            else None
        )
        if not self.verify_auth(auth_header=auth_header, key=key):
            return PaymeError(
                ERR_INSUFFICIENT_PRIVILEGE,
                _msg("Insufficient privilege to perform this method."),
            ).as_rpc(rpc_id)

        # Never execute a money transition for a malformed JSON-RPC envelope.
        # Silently replacing an attacker-controlled id with null after processing
        # would leave the provider unable to correlate the side effect or retry.
        rpc_version = body.get("jsonrpc")
        # Payme's Merchant API names the wire format RPC and requires
        # method/params/id, but its official examples omit a jsonrpc member. If a
        # version is supplied, accept only 2.0; omission remains provider-compatible.
        if rpc_id is None or (rpc_version is not None and rpc_version != "2.0"):
            return PaymeError(ERR_PARSE, _msg("Invalid JSON-RPC request.")).as_rpc(None)

        method = body.get("method")
        raw_params = body.get("params")
        if not isinstance(method, str) or len(method) > 64 or not isinstance(raw_params, dict):
            return PaymeError(ERR_PARSE, _msg("Invalid JSON-RPC request.")).as_rpc(rpc_id)
        params = raw_params
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
        payme_id = self._transaction_id(params)
        account = self._account(params)
        invoice = store.find_account(account)
        self._assert_amount(params, store, invoice)
        time_ms = self._timestamp(params.get("time"))
        existing = store.get_transaction(payme_id)
        if existing is not None:
            # Payme retries are idempotent only for the SAME immutable intent.
            # Official conformance requires the merchant to repeat its basic
            # checks; blindly echoing an id reused for a different account,
            # amount, or creation time can acknowledge the wrong payment.
            store.validate_existing_create(
                existing,
                time_ms=time_ms,
                amount_tiyin=self._amount(params),
                account=account,
                invoice=invoice,
            )
            return self._txn_create_result(existing)

        txn = store.create_transaction(
            payme_id=payme_id,
            time_ms=time_ms,
            amount_tiyin=self._amount(params),
            account=account,
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
        reason = params.get("reason", 0)
        if isinstance(reason, bool) or not isinstance(reason, int) or reason < 0 or reason > 255:
            raise PaymeError(ERR_PARSE, _msg("Invalid parameters."))
        txn = store.cancel_transaction(txn, reason=reason)
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
        frm = self._timestamp(params.get("from"))
        to = self._timestamp(params.get("to"))
        if to < frm or to - frm > _MAX_STATEMENT_WINDOW_MS:
            raise PaymeError(ERR_PARSE, _msg("Invalid statement period."))
        return {"transactions": store.statement(frm=frm, to=to)}

    # ----- helpers ---------------------------------------------------------
    def _assert_amount(self, params: dict[str, Any], store: PaymeStore, invoice: Any) -> None:
        expected = store.expected_amount_tiyin(invoice)
        if self._amount(params) != expected:
            raise PaymeError(ERR_INVALID_AMOUNT, _msg("Incorrect amount."))

    def _require_txn(self, params: dict[str, Any], store: PaymeStore) -> Any:
        txn = store.get_transaction(self._transaction_id(params))
        if txn is None:
            raise PaymeError(ERR_TRANSACTION_NOT_FOUND, _msg("Transaction not found."))
        return txn

    @staticmethod
    def _transaction_id(params: dict[str, Any]) -> str:
        value = params.get("id")
        if not isinstance(value, str) or not value or len(value) > _MAX_PAYME_ID_LENGTH:
            raise PaymeError(ERR_PARSE, _msg("Invalid transaction identifier."))
        if value.strip() != value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise PaymeError(ERR_PARSE, _msg("Invalid transaction identifier."))
        return value

    @staticmethod
    def _amount(params: dict[str, Any]) -> int:
        value = params.get("amount")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value >= 10**18:
            raise PaymeError(ERR_PARSE, _msg("Invalid payment amount."))
        return value

    @staticmethod
    def _timestamp(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 10**12 or value >= 10**14:
            raise PaymeError(ERR_PARSE, _msg("Invalid timestamp."))
        return value

    @staticmethod
    def _account(params: dict[str, Any]) -> dict[str, Any]:
        value = params.get("account")
        if not isinstance(value, dict) or not value:
            raise PaymeError(ERR_ACCOUNT_NOT_FOUND, _msg("Account is required."), data="order_id")
        return value

    @staticmethod
    def _txn_create_result(txn: Any) -> dict[str, Any]:
        return {
            "create_time": txn.create_time_ms,
            "transaction": txn.provider_txn_id,
            "state": txn.provider_state,
        }

    def build_checkout(self, *, amount_tiyin: int, account: dict[str, Any], config: Any) -> dict[str, Any]:
        from infrastructure.http_client import validate_https_endpoint

        merchant = str(getattr(config, "payme_merchant_id", ""))
        if _SAFE_CHECKOUT_VALUE.fullmatch(merchant) is None:
            raise ValueError("Payme merchant configuration is invalid.")
        normalized_account: dict[str, str] = {}
        for key, value in account.items():
            key_text = str(key)
            value_text = str(value)
            if (
                _SAFE_CHECKOUT_VALUE.fullmatch(key_text) is None
                or _SAFE_CHECKOUT_VALUE.fullmatch(value_text) is None
            ):
                raise ValueError("Payme checkout account is invalid.")
            normalized_account[key_text] = value_text
        acct = ";".join(f"ac.{key}={value}" for key, value in normalized_account.items())
        raw = f"m={merchant};{acct};a={amount_tiyin}"
        token = base64.b64encode(raw.encode()).decode()
        base = validate_https_endpoint(
            settings.PAYME_CHECKOUT_URL,
            allowed_hosts=getattr(settings, "PAYME_CHECKOUT_ALLOWED_HOSTS", ()),
        ).rstrip("/")
        return {"redirect_url": f"{base}/{quote(token, safe='=')}", "rpc_payload": None}


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
