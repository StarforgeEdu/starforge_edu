#!/usr/bin/env python3
"""Emit privacy-safe Redis/Celery broker depth evidence.

The production broker has a dedicated Redis database.  This script reads only
key types and queue lengths; it never deserializes task bodies, prints broker
credentials, or exposes queued arguments.
"""

from __future__ import annotations

import json
import os
import re
import sys
from argparse import ArgumentParser
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit

from django.conf import settings
from redis import Redis

_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_SCANNED_KEYS = 10_000
_NON_QUEUE_LIST_KEYS = frozenset({"starforge:dlq"})


def _queue_names() -> list[str]:
    names = {str(settings.CELERY_TASK_DEFAULT_QUEUE)}
    for route in settings.CELERY_TASK_ROUTES.values():
        queue = route.get("queue")
        if queue:
            names.add(str(queue))
    return sorted(names)


def _parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-empty",
        action="store_true",
        help="return 75 unless every ready queue and Redis unacknowledged set is empty",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    revision = os.environ.get("STARFORGE_RELEASE_REVISION", "")
    if not _REVISION_RE.fullmatch(revision):
        raise SystemExit("STARFORGE_RELEASE_REVISION must be the exact 40-character revision")

    broker_url = str(settings.CELERY_BROKER_URL)
    if urlsplit(broker_url).scheme not in {"redis", "rediss"}:
        raise SystemExit("Broker-depth evidence supports only the production Redis broker")

    client = cast(
        Any,
        Redis.from_url(
            broker_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
        ),
    )
    try:
        client.ping()
        queues = _queue_names()
        queue_depths = {queue: int(client.llen(queue)) for queue in queues}

        unexpected_list_count = 0
        unexpected_list_depth = 0
        dlq_depth = 0
        scanned = 0
        for key in client.scan_iter(count=500):
            scanned += 1
            if scanned > _MAX_SCANNED_KEYS:
                raise SystemExit("Broker contains too many keys for bounded release evidence")
            if key in queues or client.type(key) != "list":
                continue
            if key in _NON_QUEUE_LIST_KEYS:
                dlq_depth += int(client.llen(key))
                continue
            unexpected_list_count += 1
            unexpected_list_depth += int(client.llen(key))

        seconds, microseconds = client.time()
        captured_at = datetime.fromtimestamp(
            int(seconds) + int(microseconds) / 1_000_000,
            tz=UTC,
        ).isoformat()
        ready_total = sum(queue_depths.values()) + unexpected_list_depth
        unacknowledged = int(client.hlen("unacked"))
        unacknowledged_index = int(client.zcard("unacked_index"))
        empty = ready_total == 0 and unacknowledged == 0 and unacknowledged_index == 0
        payload = {
            "revision": revision,
            "captured_at": captured_at,
            "queue_depths": queue_depths,
            "ready_total": ready_total,
            "unacknowledged": unacknowledged,
            "unacknowledged_index": unacknowledged_index,
            "unexpected_list_queue_count": unexpected_list_count,
            "unexpected_list_queue_depth": unexpected_list_depth,
            "dead_letter_depth": dlq_depth,
            "broker_database_keys": int(client.dbsize()),
            "empty": empty,
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        if args.require_empty and not empty:
            print("Celery broker is not quiescent.", file=sys.stderr)
            return 75
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
