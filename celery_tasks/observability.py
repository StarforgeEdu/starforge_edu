"""Celery observability: DLQ on exhausted failure + task duration logging (D4-LF-5).

TASKS §22 asks for (a) a dead-letter queue so a task that exhausts its retries is
not silently lost, and (b) per-task duration metrics, both tenant-tagged.

Wiring lives in ``config/celery.py`` (off-limits to Lane F) via a single call to
``connect_celery_observability(app)`` — returned as integration_needed. The
handler bodies live HERE so they are unit-testable without a running broker.

DLQ: on ``task_failure`` (Celery fires this only AFTER retries are exhausted —
a retry raises ``Retry`` which is not a failure), push a JSON record to the Redis
list ``starforge:dlq`` (LPUSH; drain with ``LRANGE``/``RPOP``). Best-effort: a
Redis hiccup must never mask the original task error, so the push is wrapped.

Duration: ``task_prerun`` stamps a monotonic start on the request; ``task_postrun``
logs ``task=<name> state=<state> duration_ms=<n>`` on the ``starforge.celery``
logger, which carries the tenant schema via the existing ``TenantSchemaFilter``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

logger = logging.getLogger("starforge.celery")

DLQ_KEY = "starforge:dlq"
_START_ATTR = "_starforge_started_monotonic"


def _current_schema() -> str:
    """Active tenant schema name, or '-' when none (best-effort, never raises)."""
    try:
        from core.utils import current_schema

        return current_schema() or "-"
    except Exception:
        return "-"


def push_to_dlq(*, task_name: str, task_id: str | None, args: Any, kwargs: Any, exc: BaseException) -> bool:
    """LPUSH one failure record to the Redis DLQ. Returns True on success.

    Never raises: a DLQ write failure must not compound the task failure.
    """
    record = {
        "task": task_name,
        "task_id": task_id,
        "args": _json_safe(args),
        "kwargs": _json_safe(kwargs),
        "exc": f"{type(exc).__name__}: {exc}",
        "schema": _current_schema(),
        "ts": time.time(),
    }
    try:
        from infrastructure.cache.redis_client import get_redis

        get_redis().lpush(DLQ_KEY, json.dumps(record, ensure_ascii=False, default=str))
        return True
    except Exception:  # pragma: no cover - defensive; DLQ must never re-raise
        logger.exception("DLQ push failed for task %s", task_name)
        return False


def on_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, **_extra) -> None:
    """``task_failure`` receiver: retries are exhausted by the time this fires."""
    task_name = getattr(sender, "name", None) or str(sender)
    push_to_dlq(
        task_name=task_name,
        task_id=task_id,
        args=args,
        kwargs=kwargs,
        exc=exception or Exception("unknown"),
    )
    logger.error("task %s failed (id=%s) -> DLQ: %s", task_name, task_id, exception)


def on_task_prerun(sender=None, task_id=None, task=None, **_extra) -> None:
    """Stamp a monotonic start time on the task request for duration logging."""
    request = getattr(task, "request", None)
    if request is not None:
        # request may be read-only in odd cases; a missing start just yields
        # duration_ms=None in postrun (still useful).
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            setattr(request, _START_ATTR, time.monotonic())


def on_task_postrun(sender=None, task_id=None, task=None, state=None, **_extra) -> None:
    """Log structured, tenant-tagged duration on task completion (any state)."""
    request = getattr(task, "request", None)
    started = getattr(request, _START_ATTR, None) if request is not None else None
    duration_ms = round((time.monotonic() - started) * 1000, 1) if started is not None else None
    task_name = getattr(sender, "name", None) or str(sender)
    logger.info(
        "task=%s id=%s state=%s duration_ms=%s",
        task_name,
        task_id,
        state,
        duration_ms,
    )


def connect_celery_observability(app) -> None:
    """Connect the DLQ + duration handlers to one Celery app's signals.

    Called from ``config/celery.py`` after the app is built (integration_needed).
    Idempotent: Celery signals dedupe identical (receiver, sender) connections.
    """
    from celery.signals import task_failure, task_postrun, task_prerun

    task_failure.connect(on_task_failure, weak=False, dispatch_uid="starforge.dlq")
    task_prerun.connect(on_task_prerun, weak=False, dispatch_uid="starforge.duration.prerun")
    task_postrun.connect(on_task_postrun, weak=False, dispatch_uid="starforge.duration.postrun")


def _json_safe(value: Any) -> Any:
    """Best-effort JSON-serializable copy (args/kwargs may hold odd objects)."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)
