"""DLQ + duration logging handlers (D4-LF-5).

The handlers live in ``celery_tasks/observability.py`` (Lane F owns it); the
wiring is one call in ``config/celery.py`` (off-limits → integration_needed).
These tests exercise the handler bodies directly so the contract is proven
without a running broker:

* a forced task failure lands exactly one entry on the Redis DLQ list, carrying
  the task name, exception, and tenant schema;
* ``task_prerun`` -> ``task_postrun`` logs a structured, tenant-tagged duration;
* a DLQ Redis hiccup is swallowed (never compounds the original failure).
"""

from __future__ import annotations

import json
import logging
import time

from celery_tasks import observability


class _FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])


class _Sender:
    def __init__(self, name):
        self.name = name


def test_task_failure_pushes_one_dlq_entry(monkeypatch):
    fake = _FakeRedis()
    # get_redis is imported lazily inside push_to_dlq from the redis_client module.
    import infrastructure.cache.redis_client as rc

    monkeypatch.setattr(rc, "get_redis", lambda: fake)

    observability.on_task_failure(
        sender=_Sender("celery_tasks.demo.boom"),
        task_id="abc-123",
        exception=ValueError("kaboom"),
        args=(1, 2),
        kwargs={"x": "y"},
    )

    dlq = fake.lists.get(observability.DLQ_KEY)
    assert dlq is not None, "expected a DLQ list"
    assert len(dlq) == 1, "expected exactly one DLQ entry"
    record = json.loads(dlq[0])
    assert record["task"] == "celery_tasks.demo.boom"
    assert record["task_id"] == "abc-123"
    assert "kaboom" in record["exc"]
    assert "schema" in record  # tenant-tagged


def test_dlq_push_swallows_redis_errors(monkeypatch):
    import infrastructure.cache.redis_client as rc

    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(rc, "get_redis", _boom)
    # Must not raise even though Redis is unavailable.
    ok = observability.push_to_dlq(task_name="t", task_id="1", args=(), kwargs={}, exc=ValueError("x"))
    assert ok is False


def test_duration_logged_on_postrun():
    """prerun -> postrun emits a structured, tenant-tagged duration line.

    The ``starforge.celery`` logger has ``propagate=False`` (LOGGING config), so
    caplog's root handler never sees the record — attach a capturing handler to
    the logger directly to assert on its real output.
    """

    class _Req:
        pass

    class _Task:
        request = _Req()

    task = _Task()
    sender = _Sender("celery_tasks.demo.work")

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("starforge.celery")
    handler = _Capture(level=logging.INFO)
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        observability.on_task_prerun(sender=sender, task_id="t1", task=task)
        # Ensure a measurable, non-None duration.
        setattr(task.request, observability._START_ATTR, time.monotonic() - 0.01)
        observability.on_task_postrun(sender=sender, task_id="t1", task=task, state="SUCCESS")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    msgs = [r.getMessage() for r in records]
    assert any("duration_ms=" in m and "state=SUCCESS" in m for m in msgs), msgs
    # duration_ms must be a real number, not None (prerun stamped a start).
    assert not any("duration_ms=None" in m for m in msgs)


def test_connect_is_idempotent():
    """Connecting twice does not double-register (Celery dedupes by dispatch_uid)."""
    from celery.signals import task_failure

    from config.celery import app

    before = len(task_failure.receivers)
    observability.connect_celery_observability(app)
    observability.connect_celery_observability(app)
    after = len(task_failure.receivers)
    # At most one new receiver for our dispatch_uid across two connects.
    assert after - before <= 1
