"""D3-F-10 — Payme JSON-RPC golden / spec-compliance suite.

One assertion cluster per JSON-RPC method, driven by the fixtures under
``apps/payments/tests/fixtures/payme/``. Pins the Payme protocol contract Lane B
must satisfy (DAY-3.md D3-B-3):

- HTTP **200** on every call (success AND error); errors live in the JSON-RPC
  ``error`` member (the one TD-18 envelope exception, per the Lane B decision).
- HTTP Basic ``Paycom:<key>`` else ``-32504``.
- Amounts in **tiyin** (``int(total_uzs * 100)``); mismatch -> ``-31001``.
- ``account`` object passed through; unknown invoice -> a code in
  ``-31050..-31099`` whose ``data`` names the field.
- States ``1`` created, ``2`` performed, ``-1``/``-2`` cancelled; transitions
  1->2, 1->-1, 2->-2.
- All times epoch **milliseconds**.
- Unknown method -> ``-32601``; unknown transaction -> ``-31003``.
- CreateTransaction idempotent on Payme ``id``; second concurrent transaction
  for the same account -> ``-31099``.

The webhook is public-schema (TD-6): posted to the apex host, resolved by slug.
Lane code is imported lazily; the orchestrator runs this on Postgres after merge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.payments.tests import _helpers as helpers
from apps.payments.tests import builders as bld

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).parent / "fixtures" / "payme"
AMOUNT_UZS = "150000.00"
AMOUNT_TIYIN = 15_000_000  # 150000.00 * 100
ACCOUNT = {"order_id": "INV-2026-000001"}


@pytest.fixture(autouse=True)
def _map_testserver_to_public(public_tenant):
    """Webhooks are posted to the apex/``testserver`` host (public schema, TD-6).
    django-tenants only routes ``testserver`` to the public schema when a Domain
    row maps it — the root ``public_tenant`` fixture creates that mapping. Without
    it the POST 404s (no tenant for the host), not a view bug."""
    return public_tenant


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _post(center, payload, *, wrong_auth: bool = False):
    return helpers.public_client().post(
        helpers.webhook_url("payme", center.schema_name),
        data=payload,
        format="json",
        **bld.payme_auth_headers(wrong=wrong_auth),
    )


def _rpc(resp) -> dict:
    """Webhook always HTTP 200; body is JSON-RPC."""
    assert resp.status_code == 200, resp.content
    return resp.json()


def _error_code(body: dict) -> int:
    assert "error" in body, f"expected JSON-RPC error member, got {body}"
    return body["error"]["code"]


def _result(body: dict) -> dict:
    assert "result" in body, f"expected JSON-RPC result member, got {body}"
    return body["result"]


@pytest.fixture
def setup(tenant_a):
    helpers.seed_provider_configs(tenant_a)
    helpers.seed_open_invoice(tenant_a, number=ACCOUNT["order_id"], amount_uzs=AMOUNT_UZS)
    return tenant_a


# --------------------------------------------------------------------------- #
# Fixture self-consistency (no DB / lane code needed)
# --------------------------------------------------------------------------- #
def test_all_method_fixtures_present():
    expected = {
        "check_perform_transaction.json",
        "create_transaction.json",
        "perform_transaction.json",
        "cancel_transaction.json",
        "check_transaction.json",
        "get_statement.json",
        "errors.json",
    }
    present = {p.name for p in FIXTURES.glob("*.json")}
    assert expected <= present, f"missing payme fixtures: {expected - present}"


def test_tiyin_math_in_fixtures():
    fx = _load("create_transaction.json")
    assert fx["amount_tiyin"] == bld.tiyin(fx["amount_uzs"]) == AMOUNT_TIYIN


def test_fixture_error_codes_in_spec_bands():
    err = _load("errors.json")
    assert err["auth_failed"]["code"] == bld.PAYME_ERR_AUTH == -32504
    assert err["unknown_method"]["code"] == bld.PAYME_ERR_METHOD == -32601
    assert err["amount_mismatch"]["code"] == bld.PAYME_ERR_AMOUNT == -31001
    assert err["unknown_transaction"]["code"] == bld.PAYME_ERR_UNKNOWN_TXN == -31003
    band = err["account_band"]
    assert band["low"] == -31050
    assert band["high"] == -31099
    unknown = band["examples"]["unknown_invoice"]
    assert -31099 <= unknown["code"] <= -31050
    # account errors must name the offending field in `data`
    assert unknown["error"]["data"] == "order_id"


# --------------------------------------------------------------------------- #
# Auth + unknown method (don't need an invoice)
# --------------------------------------------------------------------------- #
def test_wrong_basic_auth_returns_32504(setup):
    payload = bld.payme_check_perform(amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
    body = _rpc(_post(setup, payload, wrong_auth=True))
    assert _error_code(body) == -32504


def test_unknown_method_returns_32601(setup):
    payload = bld.make_payme_rpc("NoSuchMethod", {"id": "x"})
    body = _rpc(_post(setup, payload))
    assert _error_code(body) == -32601


# --------------------------------------------------------------------------- #
# CheckPerformTransaction — amount + account band
# --------------------------------------------------------------------------- #
def test_check_perform_ok(setup):
    payload = bld.payme_check_perform(amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
    body = _rpc(_post(setup, payload))
    assert _result(body).get("allow") is True


def test_check_perform_amount_mismatch_31001(setup):
    payload = bld.payme_check_perform(amount_tiyin=AMOUNT_TIYIN + 1, account=ACCOUNT)
    body = _rpc(_post(setup, payload))
    assert _error_code(body) == -31001


def test_check_perform_unknown_account_in_31050_band(setup):
    payload = bld.payme_check_perform(amount_tiyin=AMOUNT_TIYIN, account={"order_id": "INV-NOPE-999999"})
    body = _rpc(_post(setup, payload))
    code = _error_code(body)
    assert -31099 <= code <= -31050
    # the offending account field is named in `data`
    assert body["error"].get("data") == "order_id"


# --------------------------------------------------------------------------- #
# CreateTransaction — idempotency, account passthrough, ms times, state 1
# --------------------------------------------------------------------------- #
def test_create_transaction_state_1_and_ms_times(setup):
    payload = bld.payme_create_transaction(
        payme_id="txn-golden-1", amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT, time_ms=1_700_000_000_000
    )
    body = _rpc(_post(setup, payload))
    result = _result(body)
    assert result["state"] == bld.PAYME_STATE_CREATED == 1
    # create_time is epoch milliseconds (13 digits, ~1.7e12)
    assert result["create_time"] > 1_000_000_000_000


def test_create_transaction_idempotent_one_payment(setup):
    payload = bld.payme_create_transaction(
        payme_id="txn-golden-dup", amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT
    )
    first = _result(_rpc(_post(setup, payload)))
    second = _result(_rpc(_post(setup, payload)))
    # same create_time + transaction identity on replay (idempotent on Payme id)
    assert second["create_time"] == first["create_time"]
    assert second["state"] == 1
    rows = helpers.payment_rows(setup, provider_txn_id="txn-golden-dup")
    assert len(rows) == 1


def test_create_transaction_amount_mismatch_31001(setup):
    payload = bld.payme_create_transaction(
        payme_id="txn-bad-amt", amount_tiyin=AMOUNT_TIYIN - 50, account=ACCOUNT
    )
    body = _rpc(_post(setup, payload))
    assert _error_code(body) == -31001
    assert helpers.payment_rows(setup, provider_txn_id="txn-bad-amt") == []


def test_create_transaction_second_concurrent_account_31099(setup):
    # First create succeeds for the account.
    first = bld.payme_create_transaction(payme_id="txn-acc-1", amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
    _result(_rpc(_post(setup, first)))
    # A DIFFERENT Payme id for the SAME in-progress account -> -31099.
    second = bld.payme_create_transaction(payme_id="txn-acc-2", amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
    body = _rpc(_post(setup, second))
    assert _error_code(body) == -31099


# --------------------------------------------------------------------------- #
# PerformTransaction — 1 -> 2, ms perform_time, idempotent
# --------------------------------------------------------------------------- #
def test_perform_transitions_1_to_2(setup):
    pid = "txn-perform-1"
    _result(
        _rpc(
            _post(
                setup, bld.payme_create_transaction(payme_id=pid, amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
            )
        )
    )
    body = _rpc(_post(setup, bld.payme_perform_transaction(payme_id=pid)))
    result = _result(body)
    assert result["state"] == bld.PAYME_STATE_PERFORMED == 2
    assert result["perform_time"] > 1_000_000_000_000


def test_perform_unknown_transaction_31003(setup):
    body = _rpc(_post(setup, bld.payme_perform_transaction(payme_id="never-created")))
    assert _error_code(body) == -31003


def test_perform_idempotent_single_allocation(setup):
    pid = "txn-perform-dup"
    _result(
        _rpc(
            _post(
                setup, bld.payme_create_transaction(payme_id=pid, amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
            )
        )
    )
    first = _result(_rpc(_post(setup, bld.payme_perform_transaction(payme_id=pid))))
    second = _result(_rpc(_post(setup, bld.payme_perform_transaction(payme_id=pid))))
    assert second["perform_time"] == first["perform_time"]
    assert second["state"] == 2
    # allocation ran exactly once for this payment
    pays = helpers.payment_rows(setup, provider_txn_id=pid)
    assert len(pays) == 1
    allocs = helpers.allocation_rows(setup, payment_id=pays[0].id)
    assert len(allocs) == 1


# --------------------------------------------------------------------------- #
# CancelTransaction — 1 -> -1 and 2 -> -2
# --------------------------------------------------------------------------- #
def test_cancel_before_perform_state_minus_1(setup):
    pid = "txn-cancel-pre"
    _result(
        _rpc(
            _post(
                setup, bld.payme_create_transaction(payme_id=pid, amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
            )
        )
    )
    body = _rpc(_post(setup, bld.payme_cancel_transaction(payme_id=pid, reason=3)))
    result = _result(body)
    assert result["state"] == bld.PAYME_STATE_CANCELLED_BEFORE == -1
    assert result["cancel_time"] > 1_000_000_000_000


def test_cancel_after_perform_state_minus_2(setup):
    pid = "txn-cancel-post"
    _result(
        _rpc(
            _post(
                setup, bld.payme_create_transaction(payme_id=pid, amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
            )
        )
    )
    _result(_rpc(_post(setup, bld.payme_perform_transaction(payme_id=pid))))
    body = _rpc(_post(setup, bld.payme_cancel_transaction(payme_id=pid, reason=5)))
    result = _result(body)
    assert result["state"] == bld.PAYME_STATE_CANCELLED_AFTER == -2


def test_cancel_unknown_transaction_31003(setup):
    body = _rpc(_post(setup, bld.payme_cancel_transaction(payme_id="ghost")))
    assert _error_code(body) == -31003


# --------------------------------------------------------------------------- #
# CheckTransaction + GetStatement — ms times, account echo
# --------------------------------------------------------------------------- #
def test_check_transaction_reports_ms_times_and_state(setup):
    pid = "txn-check-1"
    _result(
        _rpc(
            _post(
                setup, bld.payme_create_transaction(payme_id=pid, amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
            )
        )
    )
    _result(_rpc(_post(setup, bld.payme_perform_transaction(payme_id=pid))))
    body = _rpc(_post(setup, bld.payme_check_transaction(payme_id=pid)))
    result = _result(body)
    assert result["state"] == 2
    assert result["create_time"] > 1_000_000_000_000
    assert result["perform_time"] > 1_000_000_000_000
    # cancel_time defaults to 0 (phase not reached), never null
    assert result["cancel_time"] == 0


def test_get_statement_echoes_account_and_tiyin(setup):
    pid = "txn-stmt-1"
    _result(
        _rpc(
            _post(
                setup, bld.payme_create_transaction(payme_id=pid, amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
            )
        )
    )
    _result(_rpc(_post(setup, bld.payme_perform_transaction(payme_id=pid))))
    body = _rpc(_post(setup, bld.payme_get_statement(from_ms=1_699_999_000_000, to_ms=1_700_900_000_000)))
    txns = _result(body)["transactions"]
    mine = [t for t in txns if t.get("transaction") == pid or t.get("id") == pid]
    assert mine, f"created txn not in statement: {txns}"
    row = mine[0]
    # amount stays in tiyin and the account object is echoed back verbatim
    assert row["amount"] == AMOUNT_TIYIN
    assert row["account"] == ACCOUNT
