"""Static fail-closed contracts for the external maintenance controller."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CADDY = (ROOT / "docker" / "Caddyfile.starforge.example").read_text(encoding="utf-8")
SCRIPT = (ROOT / "scripts" / "set_production_maintenance.sh").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / "docker" / "maintenance.env.example").read_text(encoding="utf-8")


def test_caddy_has_reloadable_api_and_storage_maintenance_hooks():
    assert "import maintenance.d/state.caddy" in CADDY
    assert "import starforge_api_maintenance" in CADDY
    assert "import starforge_storage_maintenance" in CADDY
    assert CADDY.index("@health path") < CADDY.index("import starforge_api_maintenance")
    assert CADDY.index("import starforge_storage_maintenance") < CADDY.index(
        "reverse_proxy starforge-minio:9000"
    )


def test_staff_api_edge_uses_an_android_7_compatible_rsa_certificate_key():
    api_site = CADDY.split("starforge.example.com {", 1)[1].split("\n}\n\n# S3 API only.", 1)[0]

    assert "tls {\n\t\tkey_type rsa2048\n\t}" in api_site


def test_controller_blocks_all_business_ingress_but_keeps_health_and_reads():
    assert "not method GET HEAD OPTIONS" in SCRIPT
    assert 'X-StarForge-Maintenance "active"' in SCRIPT
    assert 'api_status" == "503"' in SCRIPT
    assert 'ws_status" == "503"' in SCRIPT
    assert 'storage_write_status" == "503"' in SCRIPT
    assert 'health_status" == "200"' in SCRIPT
    assert 'storage_read_status" != "503"' in SCRIPT
    assert "--proto '=https'" in SCRIPT
    assert "--connect-timeout" in SCRIPT
    assert "--max-time" in SCRIPT


def test_controller_requires_exact_digest_container_and_read_only_config_mount():
    assert "sf_require_private_root_file" in SCRIPT
    assert "sf_read_env_values" in SCRIPT
    assert "sf_require_digest_image CADDY_IMAGE" in SCRIPT
    assert "Caddy container name did not resolve exactly" in SCRIPT
    assert "--format '{{.State.Status}}'" in SCRIPT
    assert '== "running"' in SCRIPT
    assert '"bind ${CADDY_CONFIG_DIR} false"' in SCRIPT
    assert "caddy validate" in SCRIPT
    assert "caddy reload" in SCRIPT
    assert "previous state was restored" in SCRIPT


def test_maintenance_environment_is_explicit_and_backed_up_with_deployment_state():
    assert "STARFORGE_CADDY_CONTAINER=" in ENV_EXAMPLE
    assert "CADDY_IMAGE=" in ENV_EXAMPLE
    assert "@sha256:" in ENV_EXAMPLE
    assert "STARFORGE_CADDY_CONFIG_DIR=/root/starforge-deploy/caddy" in ENV_EXAMPLE
    assert "STARFORGE_API_ORIGIN=https://" in ENV_EXAMPLE
    assert "STARFORGE_MEDIA_ORIGIN=https://" in ENV_EXAMPLE
