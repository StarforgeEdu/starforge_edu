from __future__ import annotations

import stat
from types import SimpleNamespace

import pytest


def test_notification_context_is_bounded_and_json_safe():
    from apps.notifications.services import _json_safe

    context = {f"key_{index}": "🙂" * 2000 for index in range(50)}
    context["not_finite"] = float("nan")
    safe = _json_safe(context)

    assert len(safe) == 32
    assert all(len(str(value).encode("utf-8")) <= 1024 for value in safe.values())


def test_notification_context_rejects_ambiguous_non_string_keys():
    from apps.notifications.services import _json_safe

    with pytest.raises(ValueError, match="context keys"):
        _json_safe({1: "unsafe"})  # type: ignore[dict-item]


def test_push_data_preserves_reserved_identity_and_stays_bounded():
    from celery_tasks.notification_tasks import _bounded_push_data

    notification = SimpleNamespace(pk=7, event_type="report.ready")
    data = _bounded_push_data(
        notification,
        {
            "notification_id": "attacker override",
            "thread_id": 42,
            "download_url": "https://storage.example.test/private?signature=secret",
            "contact_email": "private@example.test",
            **{f"field_{index}": "x" * 2000 for index in range(40)},
        },
        tenant_slug="tenant_a",
    )

    import json

    assert data["notification_id"] == "7"
    assert data["event_type"] == "report.ready"
    assert data["thread_id"] == "42"
    assert "download_url" not in data
    assert "contact_email" not in data
    assert not any(key.startswith("field_") for key in data)
    assert len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()) <= 3072


def test_provider_receipts_drop_unknown_and_control_character_fields():
    from celery_tasks.notification_tasks import (
        _safe_push_provider_response,
        _safe_sms_provider_response,
    )

    sms = _safe_sms_provider_response(
        {
            "status": "ok\nforged",
            "message_id": "m-1",
            "phone": "+998901234567",
            "body": "private",
        }
    )
    push = _safe_push_provider_response(
        {
            "success": True,
            "message_id": "p-1\nforged",
            "token": "secret-device-token",
            "provider_debug": "private",
        },
        device_id="device-1",
    )

    assert set(sms) == {"status", "id", "message_id", "mock"}
    assert "phone" not in sms
    assert "body" not in sms
    assert "\n" not in (sms["status"] or "")
    assert set(push) == {"device_id", "success", "message_id", "error", "mock"}
    assert "token" not in push
    assert "provider_debug" not in push
    assert "\n" not in (push["message_id"] or "")


def test_backfill_report_is_private_atomic_and_does_not_follow_symlink(tmp_path):
    from apps.notifications.management.commands.backfill_notification_principals import (
        _write_private_report,
    )

    protected = tmp_path / "protected.txt"
    protected.write_text("do not replace\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.symlink_to(protected)

    _write_private_report(report, {"rows": [{"id": 1}]})

    assert protected.read_text(encoding="utf-8") == "do not replace\n"
    assert report.is_symlink() is False
    assert report.read_text(encoding="utf-8") == '{"rows":[{"id":1}]}\n'
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
