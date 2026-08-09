"""Static safety contracts for production backup and restore scripts."""

import json
from pathlib import Path

import pytest

from scripts.validate_restic_forget_plan import snapshots_to_forget
from scripts.verify_minio_restore import current_inventory, object_digests

ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = (ROOT / "scripts" / "backup_production.sh").read_text(encoding="utf-8")
RESTORE_SCRIPT = (ROOT / "scripts" / "verify_restore.sh").read_text(encoding="utf-8")
PRUNE_SCRIPT = (ROOT / "scripts" / "prune_production_backups.sh").read_text(encoding="utf-8")
DEPLOY_SCRIPT = (ROOT / "scripts" / "deploy_production.sh").read_text(encoding="utf-8")
BACKUP_EXAMPLE = (ROOT / "docker" / "backup.env.example").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker" / "docker-compose.production.yml").read_text(encoding="utf-8")
COMPOSE_ENV_EXAMPLE = (ROOT / "docker" / "compose.production.env.example").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_backup_mode_preserves_offsite_and_requires_hardened_local_repository():
    assert 'BACKUP_MODE="${backup_values[0]}"' in BACKUP_SCRIPT
    assert "BACKUP_MODE must be either local or offsite" in BACKUP_SCRIPT
    assert "LOCAL_BACKUP_ROOT must be an absolute non-root path" in BACKUP_SCRIPT
    assert "0:0:700" in BACKUP_SCRIPT
    assert "LOCAL_BACKUP_ROOT must be separate from deploy, repository, and staging paths" in BACKUP_SCRIPT
    assert "type=bind,src=${local_backup_root},dst=/repository" in BACKUP_SCRIPT
    assert "--network none" in BACKUP_SCRIPT


def test_backup_is_locked_capacity_gated_and_validates_dump_before_snapshot():
    lock = BACKUP_SCRIPT.index("flock -n 8")
    capacity = BACKUP_SCRIPT.index("require_backup_capacity preflight")
    dump = BACKUP_SCRIPT.index('pg_dump -U "$POSTGRES_USER"')
    dump_validation = BACKUP_SCRIPT.index("pg_restore --list")
    snapshot = BACKUP_SCRIPT.index('backup /backup --host "$RESTIC_HOST"')

    assert lock < capacity < dump < dump_validation < snapshot
    assert "BACKUP_STAGING_ROOT must be a dedicated mounted filesystem" in BACKUP_SCRIPT
    assert "BACKUP_MIN_FREE_BYTES" in BACKUP_SCRIPT
    assert 'mode" == "--preflight"' in BACKUP_SCRIPT


def test_backup_targets_only_the_reviewed_starforge_compose_project():
    compose_path = BACKUP_SCRIPT.index('compose_file="${REPO_DIR}/docker/docker-compose.production.yml"')
    environment_reset = BACKUP_SCRIPT.index("sf_clear_compose_process_overrides")
    compose_command = BACKUP_SCRIPT.index(
        'compose=(docker compose --env-file "$COMPOSE_ENV" -f "$compose_file")'
    )
    assert compose_path < environment_reset < compose_command
    assert "STARFORGE_COMPOSE_FILE" not in BACKUP_SCRIPT
    assert 'COMPOSE_FILE="${REPO_DIR}' not in BACKUP_SCRIPT
    assert "sf_clear_compose_process_overrides" in BACKUP_SCRIPT
    assert "sf_export_compose_infrastructure_images" in BACKUP_SCRIPT
    assert 'sf_read_env_values "$COMPOSE_ENV" compose_app_values APP_IMAGE' in BACKUP_SCRIPT
    assert 'project_name" == "starforge' in BACKUP_SCRIPT
    assert "${COMPOSE_PROJECT_NAME:-starforge}" not in BACKUP_SCRIPT


def test_backup_creates_one_atomic_staged_snapshot_with_stable_retention():
    assert 'mkdir -p "$tmp_dir/minio"' in BACKUP_SCRIPT
    assert 'mkdir -p "$tmp_dir/minio-cluster"' in BACKUP_SCRIPT
    assert 'mkdir -p "$tmp_dir/broker"' in BACKUP_SCRIPT
    assert "--rdb /backup/broker.rdb" in BACKUP_SCRIPT
    assert "redis-check-rdb" in BACKUP_SCRIPT
    assert 'mkdir -p "$tmp_dir/deployment"' in BACKUP_SCRIPT
    assert BACKUP_SCRIPT.count("backup /backup") == 1
    assert "backup /objects" not in BACKUP_SCRIPT
    assert "backup /deployment" not in BACKUP_SCRIPT
    assert '--host "$RESTIC_HOST" --tag starforge --tag production' in BACKUP_SCRIPT
    assert "object-inventory.jsonl" in BACKUP_SCRIPT
    assert "bucket-metadata.zip" in BACKUP_SCRIPT
    assert "iam-metadata.zip" in BACKUP_SCRIPT
    assert '--network "container:${minio_container}"' in BACKUP_SCRIPT
    assert "http://127.0.0.1:9000" in BACKUP_SCRIPT


def test_created_snapshot_is_promoted_only_after_exact_isolated_restore():
    integrity = BACKUP_SCRIPT.index("restic_run check --read-data")
    created = BACKUP_SCRIPT.index("${DEPLOY_DIR}/last_created_backup")
    restore = DEPLOY_SCRIPT.index('"$release_dir/scripts/verify_restore.sh"')
    promoted = DEPLOY_SCRIPT.index('atomic_marker "${DEPLOY_DIR}/last_verified_backup"', restore)

    assert integrity < created
    assert restore < promoted
    assert "last_verified_backup" not in BACKUP_SCRIPT
    assert "last_created_backup" not in RESTORE_SCRIPT


def test_retention_is_a_separate_post_promotion_operation():
    assert "forget --prune" not in BACKUP_SCRIPT
    assert "forget --prune" not in RESTORE_SCRIPT
    assert "forget --prune" not in PRUNE_SCRIPT
    assert "--group-by host,paths" in PRUNE_SCRIPT
    assert "--keep-last 5" in PRUNE_SCRIPT
    assert "--keep-daily 14" in PRUNE_SCRIPT
    assert "--keep-weekly 8" in PRUNE_SCRIPT
    assert "--keep-monthly 12" in PRUNE_SCRIPT
    assert "RESTIC_REPOSITORY_ID" in PRUNE_SCRIPT
    assert "last_created_backup" in PRUNE_SCRIPT
    assert "last_verified_backup" in PRUNE_SCRIPT
    assert 'created_snapshot" == "$verified_snapshot' in PRUNE_SCRIPT
    assert "flock -n 8" in PRUNE_SCRIPT
    assert 'snapshots --json "$verified_snapshot"' in PRUNE_SCRIPT
    assert "validate_restic_forget_plan.py" in PRUNE_SCRIPT
    assert "--dry-run --json" in PRUNE_SCRIPT
    assert 'restic_run forget "${batch[@]}"' in PRUNE_SCRIPT
    assert "restic_run prune" in PRUNE_SCRIPT
    assert "post_prune_membership" in PRUNE_SCRIPT
    assert "check --read-data-subset=5%" in PRUNE_SCRIPT

    plan = PRUNE_SCRIPT.index("--dry-run --json")
    validation = PRUNE_SCRIPT.index('python3 "$PLAN_HELPER"', plan)
    removal = PRUNE_SCRIPT.index('restic_run forget "${batch[@]}"', validation)
    prune = PRUNE_SCRIPT.index("restic_run prune", removal)
    post_prune_membership = PRUNE_SCRIPT.index("post_prune_membership", prune)
    integrity = PRUNE_SCRIPT.index("check --read-data-subset=5%", post_prune_membership)
    assert plan < validation < removal < prune < post_prune_membership < integrity


def _forget_snapshot(snapshot_id):
    return {"id": snapshot_id}


def test_retention_plan_returns_only_explicit_nonverified_removals():
    verified = f"{9:064x}"
    old_snapshot = f"{1:064x}"
    plan = [
        {
            "keep": [_forget_snapshot(verified), _forget_snapshot(f"{10:064x}")],
            "remove": [_forget_snapshot(old_snapshot)],
        }
    ]

    assert snapshots_to_forget(plan, verified_snapshot=verified) == [old_snapshot]


def test_retention_plan_rejects_verified_snapshot_after_newer_orphans():
    verified = f"{9:064x}"
    newer_orphans = [f"{number:064x}" for number in range(10, 16)]
    plan = [
        {
            "keep": [_forget_snapshot(snapshot_id) for snapshot_id in newer_orphans[-5:]],
            "remove": [
                _forget_snapshot(verified),
                _forget_snapshot(newer_orphans[0]),
            ],
        }
    ]

    with pytest.raises(ValueError, match="exact verified snapshot is selected for removal"):
        snapshots_to_forget(plan, verified_snapshot=verified)


def test_retention_plan_requires_verified_snapshot_in_keep_set():
    verified = f"{9:064x}"
    plan = [{"keep": [_forget_snapshot(f"{10:064x}")], "remove": []}]

    with pytest.raises(ValueError, match="exact verified snapshot is absent"):
        snapshots_to_forget(plan, verified_snapshot=verified)


def test_restore_uses_the_local_mount_and_one_exact_atomic_snapshot():
    assert "type=bind,src=${local_backup_root},dst=/repository" in RESTORE_SCRIPT
    assert "last_verified_backup" in RESTORE_SCRIPT
    assert 'restore "$snapshot" --host "$RESTIC_HOST" --tag starforge' in RESTORE_SCRIPT
    assert "--tag postgres" not in RESTORE_SCRIPT
    assert "--tag minio" not in RESTORE_SCRIPT
    assert "--tag configuration" not in RESTORE_SCRIPT
    assert "--no-owner --no-acl" in RESTORE_SCRIPT
    assert "--memory=384m --cpus=0.5 --pids-limit=100" in RESTORE_SCRIPT
    assert "Restored Redis snapshot is missing" in RESTORE_SCRIPT
    assert "redis-check-rdb" in RESTORE_SCRIPT
    assert 'snapshot="${snapshot:-latest}"' not in RESTORE_SCRIPT
    assert '[[ "$snapshot" =~ ^[0-9a-f]{64}$ ]]' in RESTORE_SCRIPT


def test_minio_inventory_parser_accepts_current_object_listing_shape(tmp_path):
    inventory = tmp_path / "inventory.jsonl"
    rows = [
        {
            "status": "success",
            "type": "folder",
            "key": "starforge-media/",
            "url": "http://127.0.0.1:9000/",
        },
        {
            "status": "success",
            "type": "file",
            "key": "starforge-media/tenant/report.pdf",
            "url": "http://127.0.0.1:9000/",
            "size": 42,
            "etag": "current-etag",
            # Exact pinned mc output keeps this field even without --versions.
            "versionOrdinal": 1,
        },
    ]
    inventory.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    assert current_inventory(inventory) == [("starforge-media", "tenant/report.pdf", 42)]


def test_minio_inventory_parser_rejects_version_history_and_delete_markers(tmp_path):
    inventory = tmp_path / "inventory.jsonl"
    version_rows = [
        {
            "status": "success",
            "type": "file",
            "key": "starforge-media/tenant/report.pdf",
            "size": 41,
            "etag": "old-etag",
            "versionOrdinal": 1,
        },
        {
            "status": "success",
            "type": "file",
            "key": "starforge-media/tenant/report.pdf",
            "size": 42,
            "etag": "new-etag",
            "versionOrdinal": 2,
        },
        {
            "status": "success",
            "type": "file",
            "key": "starforge-media/deleted.pdf",
            "size": 0,
            "etag": "delete-marker",
            "versionOrdinal": 3,
            "isDeleteMarker": True,
        },
    ]
    inventory.write_text("\n".join(json.dumps(row) for row in version_rows) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Version-history MinIO inventory is not accepted"):
        current_inventory(inventory)

    inventory.write_text(
        json.dumps(
            {
                "status": "success",
                "type": "file",
                "key": "starforge-media/deleted.pdf",
                "size": 0,
                "etag": "delete-marker",
                "versionOrdinal": 1,
                "isDeleteMarker": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Version-history MinIO inventory is not accepted"):
        current_inventory(inventory)


def test_minio_inventory_parser_rejects_duplicate_current_objects(tmp_path):
    inventory = tmp_path / "inventory.jsonl"
    row = {
        "status": "success",
        "type": "file",
        "key": "starforge-media/tenant/report.pdf",
        "size": 42,
        "etag": "current-etag",
    }
    inventory.write_text(f"{json.dumps(row)}\n{json.dumps(row)}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate MinIO inventory object"):
        current_inventory(inventory)


def test_minio_restore_comparison_hashes_content_not_only_size(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "same-size.bin").write_bytes(b"left")
    (second / "same-size.bin").write_bytes(b"rite")

    assert object_digests(first) != object_digests(second)


def test_backup_and_restore_capture_current_minio_view_only():
    assert "mc ls --recursive --json source/" in BACKUP_SCRIPT
    assert "mc ls --recursive --json restored/" in RESTORE_SCRIPT
    assert "mc ls --recursive --versions" not in BACKUP_SCRIPT
    assert "mc ls --recursive --versions" not in RESTORE_SCRIPT


def test_backup_helpers_are_digest_pinned_and_never_implicitly_pulled_or_initialized():
    assert "restic_run init" not in BACKUP_SCRIPT
    assert "sf_require_digest_image" in BACKUP_SCRIPT
    assert "sf_require_digest_image" in RESTORE_SCRIPT
    assert "--pull=never" in BACKUP_SCRIPT
    assert "--pull=never" in RESTORE_SCRIPT
    assert "RESTIC_REPOSITORY_ID" in BACKUP_SCRIPT
    assert "RESTIC_REPOSITORY_ID" in RESTORE_SCRIPT
    assert '[[ "$snapshot_id" =~ ^[0-9a-f]{64}$ ]]' in BACKUP_SCRIPT


def test_deployment_verifies_the_new_snapshot_before_migrations():
    backup = DEPLOY_SCRIPT.index("scripts/backup_production.sh")
    restore = DEPLOY_SCRIPT.index("scripts/verify_restore.sh")
    migrations = DEPLOY_SCRIPT.index('echo "Applying public and tenant migrations..."')
    assert backup < restore < migrations


def test_deployment_checks_repository_and_capacity_before_quiescence():
    preflight = DEPLOY_SCRIPT.index('"$release_dir/scripts/backup_production.sh" --preflight')
    quiesce = DEPLOY_SCRIPT.index("drain_project_applications \\")
    assert preflight < quiesce


def test_deployment_records_broker_depth_on_both_sides_of_quiescence():
    before = DEPLOY_SCRIPT.index(
        'capture_broker_depth "${evidence_dir}/${broker_phase}-broker-before-stop.json"'
    )
    stop = DEPLOY_SCRIPT.index("drain_project_applications \\", before)
    after = DEPLOY_SCRIPT.index("${evidence_dir}/${broker_phase}-broker-after-stop.json", stop)
    backup = DEPLOY_SCRIPT.index('"$release_dir/scripts/backup_production.sh"', after)

    assert before < stop < after < backup
    assert "scripts/capture_broker_depth.py" in DEPLOY_SCRIPT
    assert "unexpected_list_queue_depth" in (ROOT / "scripts/capture_broker_depth.py").read_text(
        encoding="utf-8"
    )


def test_deployment_blocks_producers_then_drains_workers_before_backup():
    maintenance = DEPLOY_SCRIPT.index('set_production_maintenance.sh" enable')
    drain = DEPLOY_SCRIPT.index("drain_project_applications \\")
    backup = DEPLOY_SCRIPT.index('"$release_dir/scripts/backup_production.sh"', drain)
    assert maintenance < drain < backup
    assert "{{json .Config.Cmd}}" in DEPLOY_SCRIPT
    assert 'command_json" == \'["worker"]\'' in DEPLOY_SCRIPT
    assert 'docker stop --time 120 "${producer_containers[@]}"' in DEPLOY_SCRIPT
    assert "scripts/drain_celery_for_release.py --expected-workers" in DEPLOY_SCRIPT
    assert "drained_worker_containers" in DEPLOY_SCRIPT
    assert 'actual_workers" == "$expected_workers' in DEPLOY_SCRIPT
    assert "{{.State.Status}}" in DEPLOY_SCRIPT
    assert 'docker stop --time 120 "${worker_containers[@]}"' in DEPLOY_SCRIPT
    assert 'capture_broker_depth "${evidence_prefix}-broker-empty.json" --require-empty' in DEPLOY_SCRIPT


def test_success_reopens_traffic_only_after_authenticated_leadership_smoke():
    candidate_start = DEPLOY_SCRIPT.index('up -d --remove-orphans --no-deps "${app_services[@]}"')
    smoke = DEPLOY_SCRIPT.index("scripts/run_leadership_release_smoke.py", candidate_start)
    disable = DEPLOY_SCRIPT.index('set_production_maintenance.sh" disable', smoke)
    assert_disabled = DEPLOY_SCRIPT.index('set_production_maintenance.sh" assert-disabled', disable)
    current_release = DEPLOY_SCRIPT.index('atomic_marker "${DEPLOY_DIR}/current_release"', assert_disabled)
    assert candidate_start < smoke < disable < assert_disabled < current_release
    assert 'payload["operation_count"] < 108' in DEPLOY_SCRIPT
    assert "leadership_smoke_sha256=${smoke_sha256}" in DEPLOY_SCRIPT


def test_application_release_never_creates_or_recreates_stateful_services():
    assert '"${compose[@]}" up -d postgres redis minio' not in DEPLOY_SCRIPT
    assert 'up -d --remove-orphans --no-deps "${app_services[@]}"' in DEPLOY_SCRIPT
    assert "stateful_services=(postgres redis minio)" in DEPLOY_SCRIPT
    assert DEPLOY_SCRIPT.count("verify_stateful_infrastructure") >= 3
    assert "run --rm --no-deps -T migrate" in DEPLOY_SCRIPT
    assert "run --rm --no-deps -T collectstatic" in DEPLOY_SCRIPT
    assert 'config --hash "$service"' in DEPLOY_SCRIPT
    assert "com.docker.compose.config-hash" in DEPLOY_SCRIPT
    assert ".HostConfig.PortBindings" in DEPLOY_SCRIPT
    assert "stateful_volume_destinations" in DEPLOY_SCRIPT
    assert "one reviewed writable named-volume mount" in DEPLOY_SCRIPT


def test_quiescence_discovers_renamed_and_orphan_project_containers_by_label():
    assert "label=com.docker.compose.project=${project_name}" in DEPLOY_SCRIPT
    assert 'index .Config.Labels "com.docker.compose.service"' in DEPLOY_SCRIPT
    assert 'if ! is_stateful_service "$service"' in DEPLOY_SCRIPT
    assert 'docker stop --time 120 "${running_project_applications[@]}"' in DEPLOY_SCRIPT
    assert 'compose[@]}" stop --timeout 120 "${app_services[@]}' not in DEPLOY_SCRIPT


def test_producer_stop_grace_exceeds_the_validated_request_envelope():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "gunicorn_graceful_timeout >= gunicorn_timeout + 15" in entrypoint
    assert '--graceful-timeout "$gunicorn_graceful_timeout"' in entrypoint
    assert "stop_grace_period: 120s" in COMPOSE
    assert DEPLOY_SCRIPT.count("docker stop --time 120") >= 3


def test_deployment_network_gates_are_https_only_and_bounded():
    github_gate = DEPLOY_SCRIPT.index('"https://api.github.com/repos/')
    external_readiness = DEPLOY_SCRIPT.index('"$HEALTH_URL" >/dev/null')

    for position in (github_gate, external_readiness):
        command_start = DEPLOY_SCRIPT.rfind("curl ", 0, position)
        command = DEPLOY_SCRIPT[command_start:position]
        assert "--proto '=https'" in command
        assert "--tlsv1.2" in command
        assert "--connect-timeout" in command
        assert "--max-time" in command


def test_ci_bearer_never_appears_in_a_process_argument():
    assert '-H "Authorization: Bearer ${GITHUB_TOKEN}"' not in DEPLOY_SCRIPT
    assert '--config <(printf \'header = "Authorization: Bearer %s"' in DEPLOY_SCRIPT
    assert ".github-curl" not in DEPLOY_SCRIPT


def test_backup_environment_documents_offsite_default_and_local_fallback():
    assert "BACKUP_MODE=offsite" in BACKUP_EXAMPLE
    assert "RESTIC_HOST=starforge-production" in BACKUP_EXAMPLE
    assert "# BACKUP_MODE=local" in BACKUP_EXAMPLE
    assert "# LOCAL_BACKUP_ROOT=/var/backups/starforge" in BACKUP_EXAMPLE
    assert "# RESTIC_REPOSITORY=/repository/restic" in BACKUP_EXAMPLE


def test_production_secret_paths_share_one_backed_up_deployment_root():
    for obsolete in (
        "STARFORGE_APP_ENV_FILE",
        "STARFORGE_DB_ENV_FILE",
        "STARFORGE_MINIO_ENV_FILE",
        "STARFORGE_FIREBASE_CREDENTIALS_FILE",
    ):
        assert obsolete not in COMPOSE
        assert obsolete not in COMPOSE_ENV_EXAMPLE
    assert COMPOSE.count("${STARFORGE_DEPLOY_DIR:-/root/starforge-deploy}") >= 4
    assert "STARFORGE_DEPLOY_DIR=/root/starforge-deploy" in COMPOSE_ENV_EXAMPLE


def test_runtime_image_allowlists_only_the_required_release_probe_script():
    exclude_scripts = DOCKERIGNORE.index("scripts/*")
    include_probe = DOCKERIGNORE.index("!scripts/capture_broker_depth.py")
    include_drain = DOCKERIGNORE.index("!scripts/drain_celery_for_release.py")

    assert exclude_scripts < include_probe < include_drain
    assert "scripts/" not in DOCKERIGNORE
