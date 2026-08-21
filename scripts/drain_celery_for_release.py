#!/usr/bin/env python3
"""Wait for a privacy-safe, provably empty Celery migration boundary.

Run this from the candidate application image *after* every HTTP/WebSocket/beat
producer has been stopped, while all previously running workers remain alive.
The caller discovers worker containers by Docker Compose labels/command (not by
hard-coded service names) and passes that count with ``--expected-workers``.

This program never revokes, purges, terminates, or shuts down a task. It waits
long enough for the configured 30-minute hard limit, includes worker-reserved
and ETA/retry work, inspects every Redis queue (including obsolete route names),
and succeeds only after multiple stable all-zero observations. A timeout leaves
the workers untouched and fails the release closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit

import django
from redis import Redis

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from django.conf import settings  # noqa: E402

from config.celery import app  # noqa: E402
from scripts.capture_broker_depth import (  # noqa: E402
    _MAX_SCANNED_KEYS,
    _NON_QUEUE_LIST_KEYS,
    _queue_names,
)

_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_EMPTY_OBSERVATIONS_MIN = 2


@dataclass(frozen=True)
class DrainObservation:
    captured_at: str
    worker_count: int
    worker_fingerprint: str
    active: int
    reserved: int
    scheduled: int
    ready: int
    unacknowledged: int
    unacknowledged_index: int
    unexpected_queue_count: int
    unexpected_queue_depth: int

    @property
    def empty(self) -> bool:
        return all(
            value == 0
            for value in (
                self.active,
                self.reserved,
                self.scheduled,
                self.ready,
                self.unacknowledged,
                self.unacknowledged_index,
                self.unexpected_queue_depth,
            )
        )


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-workers", type=int, required=True)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(settings.CELERY_TASK_TIME_LIMIT) + 5 * 60,
    )
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--inspect-timeout-seconds", type=float, default=8.0)
    parser.add_argument("--stable-empty-observations", type=int, default=3)
    args = parser.parse_args(argv)
    if args.expected_workers < 1:
        parser.error("--expected-workers must be positive")
    if args.timeout_seconds < int(settings.CELERY_TASK_TIME_LIMIT) + 60:
        parser.error("--timeout-seconds must exceed the configured task hard limit by at least 60 seconds")
    if not 1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 1 and 60")
    if not 1 <= args.inspect_timeout_seconds <= 30:
        parser.error("--inspect-timeout-seconds must be between 1 and 30")
    if not _EMPTY_OBSERVATIONS_MIN <= args.stable_empty_observations <= 10:
        parser.error("--stable-empty-observations must be between 2 and 10")
    return args


def _worker_counts(*, expected_workers: int, timeout: float) -> tuple[dict[str, int], str]:
    discovery = app.control.inspect(timeout=timeout)
    ping = discovery.ping() or {}
    nodes = sorted(str(node) for node in ping)
    if len(nodes) != expected_workers:
        raise RuntimeError(
            f"Expected {expected_workers} worker replies but received {len(nodes)}; refusing incomplete evidence."
        )

    inspector = app.control.inspect(destination=nodes, timeout=timeout)
    replies: dict[str, dict[str, Any]] = {}
    for label, getter in (
        ("active", inspector.active),
        ("reserved", inspector.reserved),
        ("scheduled", inspector.scheduled),
    ):
        response = getter() or {}
        if set(response) != set(nodes):
            raise RuntimeError(f"Not every discovered worker replied to the {label} inspection.")
        replies[label] = response

    counts = {
        label: sum(len(items or ()) for items in response.values()) for label, response in replies.items()
    }
    fingerprint = hashlib.sha256("\0".join(nodes).encode("utf-8")).hexdigest()
    return counts, fingerprint


def _broker_counts(client: Any) -> dict[str, int]:
    queues = _queue_names()
    ready = sum(int(client.llen(queue)) for queue in queues)
    unexpected_count = 0
    unexpected_depth = 0
    scanned = 0
    for key in client.scan_iter(count=500):
        scanned += 1
        if scanned > _MAX_SCANNED_KEYS:
            raise RuntimeError("Broker contains too many keys for bounded drain evidence.")
        if key in queues or key in _NON_QUEUE_LIST_KEYS or client.type(key) != "list":
            continue
        unexpected_count += 1
        depth = int(client.llen(key))
        unexpected_depth += depth
        ready += depth
    return {
        "ready": ready,
        "unacknowledged": int(client.hlen("unacked")),
        "unacknowledged_index": int(client.zcard("unacked_index")),
        "unexpected_queue_count": unexpected_count,
        "unexpected_queue_depth": unexpected_depth,
    }


def _observe(client: Any, *, expected_workers: int, inspect_timeout: float) -> DrainObservation:
    worker, fingerprint = _worker_counts(expected_workers=expected_workers, timeout=inspect_timeout)
    broker = _broker_counts(client)
    seconds, microseconds = client.time()
    captured_at = datetime.fromtimestamp(
        int(seconds) + int(microseconds) / 1_000_000,
        tz=UTC,
    ).isoformat()
    return DrainObservation(
        captured_at=captured_at,
        worker_count=expected_workers,
        worker_fingerprint=fingerprint,
        **worker,
        **broker,
    )


def _safe_payload(*, revision: str, observation: DrainObservation, stable: int) -> str:
    return json.dumps(
        {
            "revision": revision,
            "stable_empty_observations": stable,
            **asdict(observation),
            "empty": observation.empty,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    revision = os.environ.get("STARFORGE_RELEASE_REVISION", "")
    if not _REVISION_RE.fullmatch(revision):
        raise SystemExit("STARFORGE_RELEASE_REVISION must be the exact 40-character revision")

    broker_url = str(settings.CELERY_BROKER_URL)
    if urlsplit(broker_url).scheme not in {"redis", "rediss"}:
        raise SystemExit("Release draining supports only the production Redis broker")
    client = cast(
        Any,
        Redis.from_url(
            broker_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
        ),
    )
    deadline = time.monotonic() + args.timeout_seconds
    stable = 0
    last: DrainObservation | None = None
    try:
        client.ping()
        while time.monotonic() < deadline:
            try:
                current = _observe(
                    client,
                    expected_workers=args.expected_workers,
                    inspect_timeout=args.inspect_timeout_seconds,
                )
            except Exception as exc:
                # Worker identity/inspection failures are not evidence of idle.
                stable = 0
                print(f"Celery drain observation unavailable ({type(exc).__name__}).", file=sys.stderr)
            else:
                last = current
                stable = stable + 1 if current.empty else 0
                print(
                    "Celery drain: "
                    f"active={current.active} reserved={current.reserved} "
                    f"scheduled={current.scheduled} ready={current.ready} "
                    f"unacked={current.unacknowledged} stable={stable}",
                    file=sys.stderr,
                )
                if stable >= args.stable_empty_observations:
                    print(_safe_payload(revision=revision, observation=current, stable=stable))
                    return 0
            time.sleep(args.poll_seconds)
    finally:
        client.close()

    if last is not None:
        print(_safe_payload(revision=revision, observation=last, stable=stable))
    print(
        "Celery did not reach a stable empty boundary; workers were left running and no task was killed.",
        file=sys.stderr,
    )
    return 75


if __name__ == "__main__":
    raise SystemExit(main())
