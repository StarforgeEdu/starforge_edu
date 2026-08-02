from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from django.test import override_settings

from infrastructure.payments.click import RealClickClient, click_sign_string
from infrastructure.payments.payme import ERR_PARSE, RealPaymeClient
from infrastructure.payments.uzum import RealUzumClient, uzum_signature


def _click_payload(**changes):
    payload = {
        "click_trans_id": "click-1",
        "service_id": "service-1",
        "merchant_trans_id": "INV-1",
        "amount": "150000",
        "action": 0,
        "sign_time": "2026-08-02 10:00:00",
    }
    payload.update(changes)
    payload["sign_string"] = click_sign_string(
        click_trans_id=str(payload["click_trans_id"]),
        service_id=str(payload["service_id"]),
        secret_key="secret",
        merchant_trans_id=str(payload["merchant_trans_id"]),
        amount=str(payload["amount"]),
        action=int(payload["action"]),
        sign_time=str(payload["sign_time"]),
        merchant_prepare_id=str(payload.get("merchant_prepare_id", "")),
    )
    return payload


def test_click_signature_binds_tenant_service_id():
    payload = _click_payload()

    assert RealClickClient().verify_signature(
        payload=payload,
        secret_key="secret",
        expected_service_id="service-1",
    )
    assert not RealClickClient().verify_signature(
        payload=payload,
        secret_key="secret",
        expected_service_id="another-tenant-service",
    )


def test_click_rejects_noncanonical_action_and_malformed_digest():
    boolean_action = _click_payload(action=True, merchant_prepare_id="1")
    padded_action = _click_payload(action="00")
    malformed_digest = _click_payload()
    malformed_digest["sign_string"] = "z" * 32

    client = RealClickClient()
    assert not client.verify_signature(payload=boolean_action, secret_key="secret")
    assert not client.verify_signature(payload=padded_action, secret_key="secret")
    assert not client.verify_signature(payload=malformed_digest, secret_key="secret")


@override_settings(
    CLICK_CHECKOUT_URL="https://my.click.uz/services/pay",
    CLICK_CHECKOUT_ALLOWED_HOSTS=("my.click.uz",),
)
def test_click_checkout_encodes_reference_and_rejects_bad_config():
    client = RealClickClient()
    result = client.build_checkout(
        amount_uzs=100,
        merchant_trans_id="INV-1&amount=1",
        config=SimpleNamespace(click_service_id="service-1", click_merchant_id="merchant-1"),
    )
    assert "transaction_param=INV-1%26amount%3D1" in result["redirect_url"]

    bad_config = SimpleNamespace(click_service_id="service&admin=1", click_merchant_id="merchant-1")
    try:
        client.build_checkout(amount_uzs=100, merchant_trans_id="INV-1", config=bad_config)
    except ValueError:
        pass
    else:  # pragma: no cover - explicit assertion keeps the exception contract clear
        raise AssertionError("unsafe Click merchant configuration was accepted")


def test_uzum_legacy_hmac_authenticates_every_body_field():
    body = {"event_id": "event-1", "order_id": "INV-1", "amount": "100", "status": "PAID"}
    signature = uzum_signature(payload=body, api_key="secret")
    tampered = {**body, "signature": "unsigned-body-value"}

    client = RealUzumClient()
    assert client.verify_signature(payload=body, signature=signature, api_key="secret")
    assert not client.verify_signature(payload=tampered, signature=signature, api_key="secret")


def _payme_auth(key: str = "secret") -> str:
    return "Basic " + base64.b64encode(f"Paycom:{key}".encode()).decode()


class _NeverCalledStore:
    def get_transaction(self, *_args, **_kwargs):
        raise AssertionError("get_transaction unexpectedly called")

    def statement(self, *_args, **_kwargs):
        raise AssertionError("statement unexpectedly called")

    def find_account(self, *_args, **_kwargs):
        raise AssertionError("find_account unexpectedly called")


def test_payme_basic_auth_rejects_noncanonical_base64_and_whitespace():
    client = RealPaymeClient()
    assert client.verify_auth(auth_header=_payme_auth(), key="secret")
    assert not client.verify_auth(auth_header="Basic !!!not-base64!!!", key="secret")
    assert not client.verify_auth(auth_header=_payme_auth() + "\n", key="secret")
    assert not client.verify_auth(auth_header="Basic  " + _payme_auth().split(" ", 1)[1], key="secret")


def test_payme_rejects_boolean_transaction_id_and_unbounded_statement_window():
    client = RealPaymeClient()
    boolean_id = client.handle(
        body={"id": 1, "method": "PerformTransaction", "params": {"id": True}},
        auth_header=_payme_auth(),
        key="secret",
        store=_NeverCalledStore(),
    )
    oversized_statement = client.handle(
        body={
            "id": 2,
            "method": "GetStatement",
            "params": {"from": 1_700_000_000_000, "to": 1_703_000_000_001},
        },
        auth_header=_payme_auth(),
        key="secret",
        store=_NeverCalledStore(),
    )

    assert boolean_id["error"]["code"] == ERR_PARSE
    assert oversized_statement["error"]["code"] == ERR_PARSE


@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "1.0", "id": 1, "method": "GetStatement", "params": {}},
        {"jsonrpc": "2.0", "id": True, "method": "GetStatement", "params": {}},
        {"jsonrpc": "2.0", "id": 2**63, "method": "GetStatement", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": ["GetStatement"], "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "GetStatement", "params": []},
    ],
)
def test_payme_malformed_rpc_envelope_never_reaches_store(body):
    response = RealPaymeClient().handle(
        body=body,
        auth_header=_payme_auth(),
        key="secret",
        store=_NeverCalledStore(),
    )

    assert response["error"]["code"] == ERR_PARSE


@override_settings(UZUM_LEGACY_INTEGRATION_ENABLED=False)
def test_legacy_uzum_config_cannot_be_activated_when_contract_is_disabled():
    from apps.payments.services.v1.payment_service import _validate_config_state
    from core.exceptions import ValidationException

    with pytest.raises(ValidationException) as exc_info:
        _validate_config_state(
            current=None,
            changes={
                "provider": "uzum",
                "is_active": True,
                "uzum_merchant_id": "merchant",
                "uzum_api_key": "secret",
            },
        )
    assert exc_info.value.code == "provider_contract_unavailable"


def test_optional_provider_secret_rejects_controls_even_while_inactive():
    from apps.payments.services.v1.payment_service import _validate_config_state
    from core.exceptions import ValidationException

    with pytest.raises(ValidationException) as exc_info:
        _validate_config_state(
            current=None,
            changes={
                "provider": "payme",
                "is_active": False,
                "payme_test_key": "secret\nsmuggled",
            },
        )

    assert exc_info.value.code == "validation_error"
    assert "payme_test_key" in exc_info.value.fields
