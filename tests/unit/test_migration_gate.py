from __future__ import annotations

import os

import pytest

from core.migration_gate import validate_migration_evidence


def _write_evidence(path, *, revision, image_id, helpers, snapshot, broker):
    path.write_text(
        "\n".join(
            (
                "status=authorized",
                f"revision={revision}",
                f"candidate_image_id={image_id}",
                f"helpers_sha256={helpers}",
                f"verified_backup_snapshot={snapshot}",
                f"broker_evidence_sha256={broker}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o644)


def test_exact_host_migration_evidence_is_accepted(tmp_path):
    revision = "a" * 40
    image_id = "sha256:" + "b" * 64
    helpers = "c" * 64
    evidence = tmp_path / "evidence"
    _write_evidence(
        evidence,
        revision=revision,
        image_id=image_id,
        helpers=helpers,
        snapshot="d" * 64,
        broker="e" * 64,
    )

    values = validate_migration_evidence(
        evidence,
        revision=revision,
        image_revision=revision,
        candidate_image_id=image_id,
        helpers_sha256=helpers,
        expected_uid=os.getuid(),
    )

    assert values["verified_backup_snapshot"] == "d" * 64


def test_evidence_for_another_revision_or_writable_by_others_is_rejected(tmp_path):
    revision = "a" * 40
    evidence = tmp_path / "evidence"
    _write_evidence(
        evidence,
        revision=revision,
        image_id="sha256:" + "b" * 64,
        helpers="c" * 64,
        snapshot="d" * 64,
        broker="e" * 64,
    )
    with pytest.raises(ValueError, match="not authorized"):
        validate_migration_evidence(
            evidence,
            revision="f" * 40,
            image_revision="f" * 40,
            candidate_image_id="sha256:" + "b" * 64,
            helpers_sha256="c" * 64,
            expected_uid=os.getuid(),
        )

    evidence.chmod(0o666)
    with pytest.raises(ValueError, match="group/world writes"):
        validate_migration_evidence(
            evidence,
            revision=revision,
            image_revision=revision,
            candidate_image_id="sha256:" + "b" * 64,
            helpers_sha256="c" * 64,
            expected_uid=os.getuid(),
        )
