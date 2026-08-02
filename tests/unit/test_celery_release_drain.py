from __future__ import annotations

import json

import pytest

from scripts import drain_celery_for_release as drain


class _Broker:
    def __init__(self):
        self.depths = {
            "default": 2,
            "maintenance": 1,
            "renamed-worker-queue": 4,
            "starforge:dlq": 9,
        }

    def llen(self, key):
        return self.depths.get(key, 0)

    def scan_iter(self, *, count):
        assert count == 500
        return iter((*self.depths, "unacked", "unacked_index"))

    def type(self, key):
        if key in self.depths:
            return "list"
        return "hash" if key == "unacked" else "zset"

    def hlen(self, key):
        assert key == "unacked"
        return 3

    def zcard(self, key):
        assert key == "unacked_index"
        return 3


def test_broker_counts_include_obsolete_or_renamed_queue_without_counting_dlq(monkeypatch):
    monkeypatch.setattr(drain, "_queue_names", lambda: ["default", "maintenance"])

    result = drain._broker_counts(_Broker())

    assert result == {
        "ready": 7,
        "unacknowledged": 3,
        "unacknowledged_index": 3,
        "unexpected_queue_count": 1,
        "unexpected_queue_depth": 4,
    }


class _Inspector:
    def __init__(self, responses):
        self.responses = responses

    def ping(self):
        return self.responses["ping"]

    def active(self):
        return self.responses["active"]

    def reserved(self):
        return self.responses["reserved"]

    def scheduled(self):
        return self.responses["scheduled"]


class _Control:
    def __init__(self, responses):
        self.responses = responses
        self.destinations = []

    def inspect(self, *, timeout, destination=None):
        assert timeout == 4
        self.destinations.append(destination)
        return _Inspector(self.responses)


def test_worker_counts_broadcast_to_every_discovered_worker_including_renamed(monkeypatch):
    nodes = {"celery@critical-new": {}, "celery@reports-renamed": {}}
    responses = {
        "ping": nodes,
        "active": {name: ([{}] if "reports" in name else []) for name in nodes},
        "reserved": {name: [] for name in nodes},
        "scheduled": {name: [] for name in nodes},
    }
    control = _Control(responses)
    monkeypatch.setattr(drain.app, "control", control)

    counts, fingerprint = drain._worker_counts(expected_workers=2, timeout=4)

    assert counts == {"active": 1, "reserved": 0, "scheduled": 0}
    assert len(fingerprint) == 64
    assert control.destinations[0] is None
    assert set(control.destinations[1]) == set(nodes)


def test_worker_counts_fail_when_one_expected_worker_does_not_reply(monkeypatch):
    responses = {
        "ping": {"celery@only-one": {}},
        "active": {},
        "reserved": {},
        "scheduled": {},
    }
    monkeypatch.setattr(drain.app, "control", _Control(responses))

    with pytest.raises(RuntimeError, match="Expected 2 worker replies"):
        drain._worker_counts(expected_workers=2, timeout=4)


def test_empty_evidence_requires_worker_and_broker_views_to_be_zero():
    empty = drain.DrainObservation(
        captured_at="2026-08-02T00:00:00+00:00",
        worker_count=3,
        worker_fingerprint="a" * 64,
        active=0,
        reserved=0,
        scheduled=0,
        ready=0,
        unacknowledged=0,
        unacknowledged_index=0,
        unexpected_queue_count=1,
        unexpected_queue_depth=0,
    )
    pending = drain.DrainObservation(**{**empty.__dict__, "scheduled": 1})

    assert empty.empty is True
    assert pending.empty is False
    payload = json.loads(drain._safe_payload(revision="f" * 40, observation=empty, stable=3))
    assert payload["empty"] is True
    assert payload["worker_count"] == 3
    assert "celery@" not in json.dumps(payload)
