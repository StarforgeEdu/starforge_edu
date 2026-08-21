#!/usr/bin/env python3
"""Compare a captured MinIO mirror with an isolated restored MinIO instance."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def current_inventory(path: Path) -> list[tuple[str, str, int]]:
    """Return identities from an unversioned ``mc ls --recursive --json`` view.

    The logical backup contains only current object bytes.  Accepting the
    version-history output here is dangerous: MinIO's ``versionOrdinal`` grows
    from the oldest version, so treating ordinal 1 as current silently selects
    stale bytes and can resurrect a key whose latest row is a delete marker.
    Require the exact current-object listing shape instead.  Pinned ``mc``
    still labels each row in that unversioned view with ``versionOrdinal: 1``;
    that value is accepted only because the command itself omits
    ``--versions`` and therefore emits one current row per key.
    """

    rows: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        row = json.loads(line)
        if row.get("status") != "success":
            raise ValueError(f"Invalid MinIO inventory record at line {line_number}")
        if row.get("type") == "folder":
            continue
        if row.get("type") != "file":
            raise ValueError(f"Unknown MinIO inventory record at line {line_number}")
        ordinal = row.get("versionOrdinal")
        if (
            isinstance(ordinal, bool)
            or ordinal not in {None, 1}
            or row.get("isDeleteMarker") not in {None, False}
        ):
            raise ValueError(f"Version-history MinIO inventory is not accepted at line {line_number}")
        full_key = row.get("key")
        size = row.get("size")
        etag = row.get("etag")
        if not isinstance(full_key, str) or "/" not in full_key:
            raise ValueError(f"Incomplete MinIO inventory record at line {line_number}")
        bucket, key = full_key.split("/", 1)
        if (
            not bucket
            or not key
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(etag, str)
            or not etag
        ):
            raise ValueError(f"Incomplete MinIO inventory record at line {line_number}")
        identity = (bucket, key)
        if identity in seen:
            raise ValueError(f"Duplicate MinIO inventory object at line {line_number}")
        seen.add(identity)
        rows.append((bucket, key, size))
    return sorted(rows)


def object_digests(path: Path) -> dict[str, tuple[int, str]]:
    """Hash every restored object without following links or loading it whole."""

    result: dict[str, tuple[int, str]] = {}
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink():
            raise ValueError("MinIO mirror contains an unexpected symbolic link")
        if not candidate.is_file():
            continue
        digest = hashlib.sha256()
        size = 0
        with candidate.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        result[candidate.relative_to(path).as_posix()] = (size, digest.hexdigest())
    return result


def verify_policies(*, media_policy_path: Path, static_policy_path: Path, static_bucket: str) -> None:
    media_policy = json.loads(media_policy_path.read_text(encoding="utf-8"))
    if media_policy != {}:
        raise ValueError("Restored media bucket is not private")
    static_policy = json.loads(static_policy_path.read_text(encoding="utf-8"))
    expected_static_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{static_bucket}/*"],
            }
        ],
    }
    if static_policy != expected_static_policy:
        raise ValueError("Restored static bucket policy is not the exact reviewed read-only policy")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-inventory", required=True, type=Path)
    parser.add_argument("--restored-inventory", required=True, type=Path)
    parser.add_argument("--original-objects", required=True, type=Path)
    parser.add_argument("--restored-objects", required=True, type=Path)
    parser.add_argument("--media-policy", required=True, type=Path)
    parser.add_argument("--static-policy", required=True, type=Path)
    parser.add_argument("--static-bucket", required=True)
    args = parser.parse_args()

    if current_inventory(args.original_inventory) != current_inventory(args.restored_inventory):
        raise SystemExit("Isolated MinIO restore differs from the captured current-object inventory")
    if object_digests(args.original_objects) != object_digests(args.restored_objects):
        raise SystemExit("Isolated MinIO restore differs from the captured object bytes")
    try:
        verify_policies(
            media_policy_path=args.media_policy,
            static_policy_path=args.static_policy,
            static_bucket=args.static_bucket,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
