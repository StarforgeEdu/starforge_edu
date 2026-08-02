from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.run_leadership_release_smoke import _pointer, validate_config


def _configuration() -> dict:
    return {
        "version": 1,
        "tenant_host": "tenant.example.com",
        "director": {"username": "release.director", "password": "Safe-Director-Password-42"},
        "manager": {"username": "release.manager", "password": "Safe-Manager-Password-42"},
        "manager_out_of_scope_user_id": 42,
        "catalog": [
            {
                "name": f"read-{index:03d}",
                "role": "director" if index % 2 == 0 else "manager",
                "path": f"/api/v1/students/?page=1&page_size=1&smoke_slot={index}",
                "required_json_pointers": ["/success"],
            }
            for index in range(104)
        ],
    }


def test_smoke_configuration_requires_complete_unique_catalog():
    config = validate_config(_configuration())
    assert len(config["catalog"]) == 104
    assert all(operation["expected_status"] == 200 for operation in config["catalog"])

    incomplete = _configuration()
    incomplete["catalog"].pop()
    with pytest.raises(ValueError, match="at least 104"):
        validate_config(incomplete)

    duplicate = _configuration()
    duplicate["catalog"][1]["path"] = duplicate["catalog"][0]["path"]
    duplicate["catalog"][1]["role"] = duplicate["catalog"][0]["role"]
    with pytest.raises(ValueError, match="duplicates a role/path"):
        validate_config(duplicate)


@pytest.mark.parametrize(
    "path",
    [
        "https://attacker.invalid/api/v1/users/",
        "//attacker.invalid/api/v1/users/",
        "/api/v1/../platform/",
        "/api/v1/%2e%2e/platform/",
        "/api/v1/users\\anything/",
        "/api/v1/users/%00",
    ],
)
def test_smoke_configuration_rejects_path_escape_forms(path):
    config = _configuration()
    config["catalog"][0]["path"] = path
    with pytest.raises(ValueError, match="catalog path"):
        validate_config(config)


def test_smoke_configuration_rejects_placeholder_or_extra_secret_fields():
    placeholder = _configuration()
    placeholder["director"]["password"] = "REPLACE_WITH_PASSWORD"
    with pytest.raises(ValueError, match="placeholder"):
        validate_config(placeholder)

    extra = deepcopy(_configuration())
    extra["director"]["token"] = "must-not-be-accepted"
    with pytest.raises(ValueError, match="credentials"):
        validate_config(extra)


def test_json_pointer_handles_nested_arrays_and_escaping():
    document = {"data": {"items": [{"a/b": {"~key": 7}}]}}
    assert _pointer(document, "/data/items/0/a~1b/~0key") == 7
    with pytest.raises(KeyError):
        _pointer(document, "/data/items/1")
