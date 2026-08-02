"""FCM push client tests (D3-C-6).

The mock is pure-Python (NEVER imports firebase_admin), deterministic, and a
token containing "dead" reports failure so the bounce path is testable.
"""

from __future__ import annotations

from typing import Any

from infrastructure.push.fcm_client import FCMClient, MockFCMClient, PushClient, get_push_client


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


def test_real_fcm_message_has_private_chat_routing_metadata(monkeypatch):
    from firebase_admin import messaging

    captured: list[Any] = []

    def capture_send(message: Any) -> str:
        captured.append(message)
        return "fcm-1"

    client = FCMClient(credentials_file="unused-in-test.json")
    monkeypatch.setattr(client, "_ensure_app", lambda: object())
    monkeypatch.setattr(messaging, "send", capture_send)

    result = client.send(
        token="device-token",
        title="New message",
        body="You received a new message.",
        data={"thread_id": "42", "message_id": "8"},
    )

    assert result == {"success": True, "message_id": "fcm-1", "error": None}
    message = captured[0]
    assert message.data == {"thread_id": "42", "message_id": "8"}
    assert message.android.priority == "high"
    assert message.android.notification.channel_id == "starforge_messages"
    # No custom click action: Android's launcher intent is always available and
    # firebase_messaging still receives the tap payload for deep-link routing.
    assert message.android.notification.click_action is None
    assert message.android.notification.tag == "thread-42"
    assert message.apns.payload.aps.thread_id == "thread-42"
    # Privacy contract: no actual chat body is ever placed in data.
    assert "body" not in message.data
