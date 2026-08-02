"""Fail-closed validation for host-issued production migration evidence."""

from __future__ import annotations

import argparse
import re
import stat
from pathlib import Path

_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REQUIRED = {
    "status",
    "revision",
    "candidate_image_id",
    "helpers_sha256",
    "verified_backup_snapshot",
    "broker_evidence_sha256",
}


def validate_migration_evidence(
    path: Path,
    *,
    revision: str,
    image_revision: str,
    candidate_image_id: str,
    helpers_sha256: str,
    expected_uid: int = 0,
) -> dict[str, str]:
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != expected_uid or mode & 0o022:
        raise ValueError("migration evidence must be a root-owned regular file without group/world writes")

    values: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or "=" not in line:
            raise ValueError(f"malformed migration evidence line {line_number}")
        key, value = line.split("=", 1)
        if key in values or key not in _REQUIRED or not value:
            raise ValueError(f"invalid migration evidence field on line {line_number}")
        values[key] = value
    if set(values) != _REQUIRED:
        raise ValueError("migration evidence fields are incomplete")
    if not _SHA40.fullmatch(revision) or image_revision != revision:
        raise ValueError("release and image revisions do not match exactly")
    if values["status"] != "authorized" or values["revision"] != revision:
        raise ValueError("migration evidence is not authorized for this revision")
    if not _IMAGE_ID.fullmatch(candidate_image_id) or values["candidate_image_id"] != candidate_image_id:
        raise ValueError("migration evidence candidate image does not match")
    if not _SHA256.fullmatch(helpers_sha256) or values["helpers_sha256"] != helpers_sha256:
        raise ValueError("migration evidence helper manifest does not match")
    for key in ("verified_backup_snapshot", "broker_evidence_sha256"):
        if not _SHA256.fullmatch(values[key]):
            raise ValueError(f"migration evidence has an invalid {key}")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image-revision", required=True)
    parser.add_argument("--candidate-image-id", required=True)
    parser.add_argument("--helpers-sha256", required=True)
    args = parser.parse_args()
    try:
        validate_migration_evidence(
            args.evidence,
            revision=args.revision,
            image_revision=args.image_revision,
            candidate_image_id=args.candidate_image_id,
            helpers_sha256=args.helpers_sha256,
            expected_uid=0,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
