"""Eskiz client unit tests (D1-LA-3 / TD-17). No DB, no network."""

from unittest import mock

import pytest
import requests

from infrastructure.sms.eskiz_client import EskizClient, get_sms_client


def _resp(status: int, json_data: dict | None = None) -> mock.Mock:
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = json_data or {}
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
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


def test_eskiz_sender_comes_from_settings(monkeypatch, settings):
    """get_sms_client wires settings.ESKIZ_FROM into the `from` form field (TD-17)."""
    settings.ESKIZ_USE_MOCK = False
    settings.ESKIZ_FROM = "STARFORGE"

    client = get_sms_client()
    assert isinstance(client, EskizClient)
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
