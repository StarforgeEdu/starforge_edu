"""FCM push client tests (D3-C-6).

The mock is pure-Python (NEVER imports firebase_admin), deterministic, and a
token containing "dead" reports failure so the bounce path is testable.
"""

from __future__ import annotations

from infrastructure.push.fcm_client import MockFCMClient, PushClient, get_push_client


def test_get_push_client_returns_mock_by_default():
    assert isinstance(get_push_client(), PushClient)
    assert isinstance(get_push_client(), MockFCMClient)


def test_mock_send_is_deterministic():
    client = MockFCMClient()
    a = client.send(token="abc", title="t", body="b")
    b = client.send(token="abc", title="t", body="b")
    assert a["success"] is True
    assert a["message_id"] == b["message_id"]


def test_mock_dead_token_reports_failure():
    client = MockFCMClient()
    result = client.send(token="dead-token", title="t", body="b")
    assert result["success"] is False
    assert result["error"] == "unregistered"


def test_mock_outbox_captures_sends():
    MockFCMClient.outbox.clear()
    client = MockFCMClient()
    client.send(token="abc", title="hi", body="there", data={"k": "v"})
    assert MockFCMClient.outbox[-1]["title"] == "hi"
    assert MockFCMClient.outbox[-1]["token"] == "abc"


def test_fcm_module_imports_without_firebase_admin():
    """The module must load with firebase_admin absent (it is NOT installed).

    Only an actual real-mode send requires the lazy import.
    """
    import importlib

    module = importlib.import_module("infrastructure.push.fcm_client")
    assert module is not None
