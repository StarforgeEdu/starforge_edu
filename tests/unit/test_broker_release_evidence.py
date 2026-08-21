from __future__ import annotations

import json

from django.conf import settings

from scripts import capture_broker_depth


class _FakeRedis:
    depths = {
        "critical": 2,
        "default": 1,
        "notifications": 3,
        "private-route-name": 4,
        "starforge:dlq": 7,
    }

    def ping(self):
        return True

    def llen(self, key):
        return self.depths.get(key, 0)

    def scan_iter(self, *, count):
        assert count == 500
        return iter((*self.depths, "unacked", "unacked_index"))

    def type(self, key):
        return "list" if key in self.depths else ("hash" if key == "unacked" else "zset")

    def hlen(self, key):
        assert key == "unacked"
        return 5

    def zcard(self, key):
        assert key == "unacked_index"
        return 5

    def time(self):
        return (1_700_000_000, 123_000)

    def dbsize(self):
        return 6

    def close(self):
        return None


def test_broker_evidence_reports_only_counts(monkeypatch, capsys):
    fake = _FakeRedis()
    monkeypatch.setenv("STARFORGE_RELEASE_REVISION", "a" * 40)
    monkeypatch.setattr(settings, "CELERY_BROKER_URL", "redis://secret@broker.invalid:6379/2")
    monkeypatch.setattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "default")
    monkeypatch.setattr(
        settings,
        "CELERY_TASK_ROUTES",
        {
            "payments.*": {"queue": "critical"},
            "notifications.*": {"queue": "notifications"},
        },
    )
    monkeypatch.setattr(
        capture_broker_depth.Redis,
        "from_url",
        lambda *args, **kwargs: fake,
    )

    assert capture_broker_depth.main([]) == 0
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)

    assert payload["queue_depths"] == {"critical": 2, "default": 1, "notifications": 3}
    assert payload["unexpected_list_queue_count"] == 1
    assert payload["unexpected_list_queue_depth"] == 4
    assert payload["ready_total"] == 10
    assert payload["unacknowledged"] == 5
    assert payload["dead_letter_depth"] == 7
    assert payload["empty"] is False
    assert "secret" not in rendered
    assert "broker.invalid" not in rendered
    assert "private-route-name" not in rendered


def test_broker_evidence_require_empty_fails_closed(monkeypatch, capsys):
    fake = _FakeRedis()
    monkeypatch.setenv("STARFORGE_RELEASE_REVISION", "b" * 40)
    monkeypatch.setattr(settings, "CELERY_BROKER_URL", "rediss://secret@broker.invalid:6380/2")
    monkeypatch.setattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "default")
    monkeypatch.setattr(settings, "CELERY_TASK_ROUTES", {})
    monkeypatch.setattr(capture_broker_depth.Redis, "from_url", lambda *args, **kwargs: fake)

    assert capture_broker_depth.main(["--require-empty"]) == 75
    assert "not quiescent" in capsys.readouterr().err


def test_broker_evidence_accepts_empty_queues_even_with_dlq_history(monkeypatch, capsys):
    fake = _FakeRedis()
    fake.depths = {"default": 0, "starforge:dlq": 9}
    monkeypatch.setenv("STARFORGE_RELEASE_REVISION", "c" * 40)
    monkeypatch.setattr(settings, "CELERY_BROKER_URL", "redis://broker.invalid:6379/2")
    monkeypatch.setattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "default")
    monkeypatch.setattr(settings, "CELERY_TASK_ROUTES", {})
    monkeypatch.setattr(capture_broker_depth.Redis, "from_url", lambda *args, **kwargs: fake)
    monkeypatch.setattr(fake, "hlen", lambda key: 0)
    monkeypatch.setattr(fake, "zcard", lambda key: 0)

    assert capture_broker_depth.main(["--require-empty"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["empty"] is True
    assert payload["dead_letter_depth"] == 9
    assert payload["ready_total"] == 0
