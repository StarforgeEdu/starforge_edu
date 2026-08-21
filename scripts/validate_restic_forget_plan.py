#!/usr/bin/env python3
"""Validate a Restic retention plan before deleting any snapshots."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}\Z")


def _plan_snapshot_ids(rows: Any, *, group_index: int, disposition: str) -> list[str]:
    if not isinstance(rows, list):
        raise ValueError(f"retention group {group_index} has a non-list {disposition} field")

    snapshot_ids: list[str] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"retention group {group_index} {disposition} row {row_index} is not an object")
        snapshot_id = row.get("id")
        if not isinstance(snapshot_id, str) or SNAPSHOT_ID.fullmatch(snapshot_id) is None:
            raise ValueError(
                f"retention group {group_index} {disposition} row {row_index} "
                "does not contain an exact snapshot ID"
            )
        snapshot_ids.append(snapshot_id)
    return snapshot_ids


def snapshots_to_forget(plan: Any, *, verified_snapshot: str) -> list[str]:
    """Return the validated removal IDs while protecting the verified snapshot."""

    if SNAPSHOT_ID.fullmatch(verified_snapshot) is None:
        raise ValueError("verified snapshot is not an exact 64-character snapshot ID")
    if not isinstance(plan, list):
        raise ValueError("Restic forget plan must be a JSON array")

    kept: set[str] = set()
    removed: set[str] = set()
    seen: set[str] = set()
    for group_index, group in enumerate(plan):
        if not isinstance(group, dict):
            raise ValueError(f"retention group {group_index} is not an object")

        for disposition, destination in (("keep", kept), ("remove", removed)):
            for snapshot_id in _plan_snapshot_ids(
                group.get(disposition),
                group_index=group_index,
                disposition=disposition,
            ):
                if snapshot_id in seen:
                    raise ValueError(f"snapshot {snapshot_id} appears more than once in the plan")
                seen.add(snapshot_id)
                destination.add(snapshot_id)

    if verified_snapshot in removed:
        raise ValueError("the exact verified snapshot is selected for removal")
    if verified_snapshot not in kept:
        raise ValueError("the exact verified snapshot is absent from the retention keep set")

    return sorted(removed)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--verified-snapshot", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        removal_ids = snapshots_to_forget(plan, verified_snapshot=args.verified_snapshot)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Unsafe Restic forget plan: {exc}", file=sys.stderr)
        return 1

    for snapshot_id in removal_ids:
        print(snapshot_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
