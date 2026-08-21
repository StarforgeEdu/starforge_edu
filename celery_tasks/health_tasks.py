"""Runtime heartbeats proving that both Celery Beat and a worker are alive."""

from __future__ import annotations

import time

from config.celery import app
from infrastructure.cache.redis_client import get_redis

RUNTIME_HEARTBEAT_KEY = "starforge:runtime:beat-worker-heartbeat"
RUNTIME_HEARTBEAT_TTL_SECONDS = 120


@app.task(ignore_result=True)
def record_runtime_heartbeat() -> float:
    """Record a short-lived timestamp after Beat publishes and a worker executes."""
    timestamp = time.time()
    get_redis().set(RUNTIME_HEARTBEAT_KEY, str(timestamp), ex=RUNTIME_HEARTBEAT_TTL_SECONDS)
    return timestamp
