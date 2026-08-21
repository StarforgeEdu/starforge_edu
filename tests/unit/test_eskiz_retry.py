"""Eskiz client unit tests (D1-LA-3 / TD-17). No DB, no network."""

import json
from unittest import mock

import pytest
import requests

from infrastructure.sms.eskiz_client import EskizClient, MockEskizClient, get_sms_client


def _resp(status: int, json_data: dict | None = None) -> mock.Mock:
    response = mock.Mock()
    response.status_code = status
    payload = json.dumps(json_data or {}).encode()
    response.headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }
    response.iter_content.return_value = [payload]
    response.close.return_value = None
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}", response=response)
    else:
        response.raise_for_status.return_value = None
    return response


def _client() -> EskizClient:
    return EskizClient(
        base_url="https://eskiz.test/api",
        email="ops@example.com",
        password="secret",
        sender="4546",
    )


def test_disabled_sms_fails_closed_before_client_construction(settings):
    from core.exceptions import ServiceUnavailableException

    settings.SMS_ENABLED = False
    with pytest.raises(ServiceUnavailableException) as exc_info:
        get_sms_client()
    assert exc_info.value.code == "sms_unavailable"


def test_eskiz_401_reauths_exactly_once_then_raises(monkeypatch):
    """Two consecutive 401s: one re-login, one retry, then HTTPError (no recursion)."""
    client = _client()
    client._token = "stale-token"  # skip the lazy first login; isolate the re-auth path
    calls = {"login": 0, "send": 0}

    def fake_post(url, **kwargs):
        if url.endswith("/auth/login"):
            calls["login"] += 1
            return _resp(200, {"data": {"token": f"tok-{calls['login']}"}})
        calls["send"] += 1
        return _resp(401)

    monkeypatch.setattr("infrastructure.sms.eskiz_client.requests.post", fake_post)

    with pytest.raises(requests.HTTPError):
        client.send(phone="+998901234567", text="hello")

    assert calls["login"] == 1  # exactly one re-auth (TD-17 guard)
    assert calls["send"] == 2  # original attempt + single retry, then raise


def test_eskiz_401_once_recovers_after_relogin(monkeypatch):
    client = _client()
    client._token = "stale-token"
    calls = {"login": 0, "send": 0}

    def fake_post(url, **kwargs):
        if url.endswith("/auth/login"):
            calls["login"] += 1
            return _resp(200, {"data": {"token": "fresh"}})
        calls["send"] += 1
        if calls["send"] == 1:
            return _resp(401)
        assert kwargs["headers"] == {"Authorization": "Bearer fresh"}
        return _resp(200, {"status": "ok"})

    monkeypatch.setattr("infrastructure.sms.eskiz_client.requests.post", fake_post)

    assert client.send(phone="+998901234567", text="hello") == {"status": "ok"}
    assert calls["login"] == 1
    assert calls["send"] == 2


def test_eskiz_send_response_is_semantically_checked_and_privacy_minimized(monkeypatch):
    client = _client()
    client._token = "token"
    response = {
        "id": "sms-123",
        "status": "waiting",
        "message": "Waiting for SMS provider",
        "mobile_phone": "998901234567",
        "request": {"message": "private body"},
    }
    monkeypatch.setattr(
        "infrastructure.sms.eskiz_client.requests.post",
        lambda *_args, **_kwargs: _resp(200, response),
    )

    assert client.send(phone="+998901234567", text="private body") == {
        "id": "sms-123",
        "status": "waiting",
    }


@pytest.mark.parametrize("status", ["failed", "error", "unexpected"])
def test_eskiz_http_200_rejection_is_not_recorded_as_sent(monkeypatch, status):
    from infrastructure.http_client import InvalidProviderResponse

    client = _client()
    client._token = "token"
    monkeypatch.setattr(
        "infrastructure.sms.eskiz_client.requests.post",
        lambda *_args, **_kwargs: _resp(200, {"status": status}),
    )

    with pytest.raises(InvalidProviderResponse, match="did not accept"):
        client.send(phone="+998901234567", text="hello")


def test_eskiz_sender_comes_from_settings(monkeypatch, settings):
    """get_sms_client wires settings.ESKIZ_FROM into the `from` form field (TD-17)."""
    settings.ESKIZ_USE_MOCK = False
    settings.ESKIZ_FROM = "STARFORGE"

    client = get_sms_client()
    assert isinstance(client, EskizClient)
    assert get_sms_client() is client  # bearer token is reused within this worker
    assert client.sender == "STARFORGE"

    captured: dict = {}

    def fake_post(url, **kwargs):
        if url.endswith("/auth/login"):
            return _resp(200, {"data": {"token": "tok"}})
        captured.update(kwargs.get("data", {}))
        return _resp(200, {"status": "ok"})

    monkeypatch.setattr("infrastructure.sms.eskiz_client.requests.post", fake_post)

    client.send(phone="+998901234567", text="hello")
    assert captured["from"] == "STARFORGE"
    assert captured["mobile_phone"] == "998901234567"  # Eskiz wants no leading +


@pytest.mark.parametrize(
    ("phone", "text"),
    [
        pytest.param("998901234567", "hello", id="missing-country-prefix"),
        pytest.param("+++998901234567", "hello", id="repeated-plus"),
        pytest.param("+99890123456", "hello", id="short-phone"),
        pytest.param("+998901234567", "", id="empty-text"),
        pytest.param("+998901234567", " \n ", id="blank-text"),
        pytest.param("+998901234567", "hello\x00world", id="null-character"),
        pytest.param("+998901234567", "x" * 4097, id="character-limit"),
        pytest.param("+998901234567", "\U0001f642" * 4096, id="utf8-byte-limit"),
    ],
)
def test_eskiz_rejects_malformed_or_unbounded_payload_before_network(monkeypatch, phone, text):
    post = mock.Mock(side_effect=AssertionError("network must not be reached"))
    monkeypatch.setattr("infrastructure.sms.eskiz_client.requests.post", post)

    with pytest.raises(ValueError, match=r"(?:Eskiz phone|SMS text)"):
        _client().send(phone=phone, text=text)

    post.assert_not_called()


def test_mock_eskiz_enforces_the_same_outbound_payload_contract():
    client = MockEskizClient()
    with pytest.raises(ValueError, match="Eskiz phone"):
        client.send(phone="not-a-phone", text="hello")


def test_mock_eskiz_does_not_retain_message_content_outside_test_capture(settings):
    settings.SMS_MOCK_CAPTURE_OUTBOX = False
    MockEskizClient.outbox.clear()

    result = MockEskizClient().send(phone="+998901234567", text="private family message")

    assert result == {"status": "ok", "mock": True}
    assert MockEskizClient.outbox == []
