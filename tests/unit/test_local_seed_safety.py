from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.local_seed_safety import (
    LOCAL_SEED_CONFIRMATION_ENV,
    assert_local_seed_environment,
)


def _settings(*, module: str = "config.settings.development", debug: bool = True, host: str = "127.0.0.1"):
    return SimpleNamespace(
        SETTINGS_MODULE=module,
        DEBUG=debug,
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "HOST": host,
                "PORT": "5432",
            }
        },
    )


def test_debug_does_not_allow_non_development_settings(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(LOCAL_SEED_CONFIRMATION_ENV, "1")

    with pytest.raises(RuntimeError, match=r"config\.settings\.development"):
        assert_local_seed_environment(
            _settings(module="config.settings.staging", debug=True),
            project_root=tmp_path,
        )


def test_local_seed_requires_one_shot_confirmation(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(LOCAL_SEED_CONFIRMATION_ENV, raising=False)

    with pytest.raises(RuntimeError, match=LOCAL_SEED_CONFIRMATION_ENV):
        assert_local_seed_environment(_settings(), project_root=tmp_path)


def test_local_seed_rejects_public_database_target(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(LOCAL_SEED_CONFIRMATION_ENV, "1")

    with pytest.raises(RuntimeError, match="loopback or private"):
        assert_local_seed_environment(_settings(host="8.8.8.8"), project_root=tmp_path)


def test_local_seed_accepts_confirmed_private_development_target(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(LOCAL_SEED_CONFIRMATION_ENV, "1")

    assert_local_seed_environment(_settings(host="10.10.0.8"), project_root=tmp_path)
