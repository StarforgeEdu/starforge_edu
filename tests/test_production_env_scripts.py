"""Executable guards for root-only production environment parsing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "lib" / "production_env.sh"


def _read_value(path: Path, key: str = "TARGET") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; if ! sf_read_env_values "$2" values "$3"; then exit 1; fi; '
            'printf "%s" "${values[0]}"',
            "starforge-env-test",
            str(HELPER),
            str(path),
            key,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_environment_reader_returns_literal_data_without_evaluation(tmp_path):
    path = tmp_path / "app.env"
    path.write_text("TARGET=https://example.invalid/path?a=b&c=d\n", encoding="utf-8")

    result = _read_value(path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "https://example.invalid/path?a=b&c=d"


@pytest.mark.parametrize(
    "content",
    [
        "TARGET=first\nTARGET=second\n",
        "export TARGET=value\n",
        " TARGET=value\n",
        "TARGET='quoted'\n",
        "TARGET=value with spaces\n",
    ],
)
def test_environment_reader_rejects_ambiguous_or_shell_grammar(tmp_path, content):
    path = tmp_path / "app.env"
    path.write_text(content, encoding="utf-8")

    result = _read_value(path)

    assert result.returncode != 0


def test_environment_reader_never_executes_command_substitution(tmp_path):
    evidence = tmp_path / "executed"
    path = tmp_path / "app.env"
    path.write_text(f"TARGET=$(touch {evidence})\n", encoding="utf-8")

    result = _read_value(path)

    assert result.returncode != 0
    assert not evidence.exists()


def test_environment_reader_never_persists_secret_material_under_hostile_tmpdir(tmp_path):
    hostile_tmp = tmp_path / "deployment-tree"
    hostile_tmp.mkdir()
    secret = "production-secret-that-must-remain-in-memory"
    path = tmp_path / "app.env"
    path.write_text(f"TARGET={secret}\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["TMPDIR"] = str(hostile_tmp)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; sf_read_env_values "$2" values TARGET; printf "%s" "${values[0]}"',
            "starforge-env-test",
            str(HELPER),
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == secret
    assert list(hostile_tmp.iterdir()) == []
    assert secret.encode() not in b"".join(
        candidate.read_bytes() for candidate in hostile_tmp.rglob("*") if candidate.is_file()
    )


def test_private_file_guard_rejects_symlink_and_broad_mode(tmp_path):
    target = tmp_path / "secret.env"
    target.write_text("TARGET=value\n", encoding="utf-8")
    target.chmod(0o644)
    symlink = tmp_path / "secret-link.env"
    symlink.symlink_to(target)

    for path in (target, symlink):
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; sf_require_private_root_file "$2"',
                "starforge-env-test",
                str(HELPER),
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


def test_private_file_guard_rejects_non_root_owner(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("The test runner itself owns fixtures as root")
    path = tmp_path / "secret.env"
    path.write_text("TARGET=value\n", encoding="utf-8")
    path.chmod(0o600)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; sf_require_private_root_file "$2"',
            "starforge-env-test",
            str(HELPER),
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    "image",
    [
        "redis:7-alpine",
        "redis@sha256:short",
        "redis@sha512:" + "a" * 64,
        "redis@sha256:" + "g" * 64,
    ],
)
def test_digest_guard_rejects_mutable_or_malformed_images(image):
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; sf_require_digest_image TEST_IMAGE "$2"',
            "starforge-env-test",
            str(HELPER),
            image,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


def test_compose_image_export_overrides_conflicting_inherited_values(tmp_path):
    digest = "a" * 64
    path = tmp_path / "compose.env"
    path.write_text(
        "\n".join(
            [
                f"POSTGRES_IMAGE=postgres@sha256:{digest}",
                f"REDIS_IMAGE=redis@sha256:{digest}",
                f"MINIO_IMAGE=minio@sha256:{digest}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_IMAGE": "attacker.invalid/postgres@sha256:" + "b" * 64,
            "REDIS_IMAGE": "attacker.invalid/redis@sha256:" + "b" * 64,
            "MINIO_IMAGE": "attacker.invalid/minio@sha256:" + "b" * 64,
            "COMPOSE_FILE": "/tmp/unreviewed.yml",
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; sf_clear_compose_process_overrides; '
            'sf_export_compose_infrastructure_images "$2"; '
            'printf "%s\\n%s\\n%s\\n%s" "$POSTGRES_IMAGE" "$REDIS_IMAGE" '
            '"$MINIO_IMAGE" "${COMPOSE_FILE-unset}"',
            "starforge-env-test",
            str(HELPER),
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"postgres@sha256:{digest}",
        f"redis@sha256:{digest}",
        f"minio@sha256:{digest}",
        "unset",
    ]
