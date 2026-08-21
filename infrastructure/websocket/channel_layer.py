"""Helpers for sending messages into the Channels layer from sync code."""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def group_send(group: str, message: dict[str, Any]) -> None:
    """Best-effort realtime broadcast into the Channels group.

    A realtime push is NOT the system of record — the durable Notification /
    AttendanceRecord row is committed to the DB before this is ever called. So a
    channel-layer outage (Redis down, connection refused) must never propagate:
    these calls run inside ``transaction.on_commit`` hooks, where a raised
    exception both 500s the already-committed request AND aborts every remaining
    on_commit callback (dropping the guardian notifications of every later absent
    student in the same batch). Swallow and log instead — the client reconnect /
    the persisted row is the fallback.
    """
    try:
        layer = get_channel_layer()
        if layer is None:  # no channel layer configured (e.g. a management command)
            return
        async_to_sync(layer.group_send)(group, message)
    except Exception:  # realtime push is best-effort by contract
        logger.warning("channel_layer.group_send failed for group %s", group, exc_info=True)
