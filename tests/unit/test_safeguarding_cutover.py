"""Non-database guards for the maintenance-only encryption deployment."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from django.db.migrations.loader import MigrationLoader
from django.db.models import NOT_PROVIDED

from apps.tenancy.management.commands.check_safeguarding_encryption_cutover import (
    LEGACY_TENANT_ANCHORS,
    REQUIRED_MIGRATIONS,
    requires_safeguarding_cutover,
)

ROOT = Path(__file__).resolve().parents[2]


def test_cutover_state_requires_every_maintenance_only_tenant_migration():
    assert ("payments", "0007_webhook_privacy_and_txn_integrity") in REQUIRED_MIGRATIONS
    assert ("payments", "0008_external_provider_transaction_integrity") in REQUIRED_MIGRATIONS
    assert ("notifications", "0012_recipient_principal_attribution") in REQUIRED_MIGRATIONS
    assert ("audit", "0005_audit_scope_snapshot") in REQUIRED_MIGRATIONS
    assert ("reports", "0006_report_scope_params_indexes") in REQUIRED_MIGRATIONS
    assert ("org", "0020_org_scope_and_history_integrity") in REQUIRED_MIGRATIONS
    assert ("org", "0021_durable_center_settings") in REQUIRED_MIGRATIONS
    assert ("parents", "0010_preserve_family_lifecycle_history") in REQUIRED_MIGRATIONS
    assert ("students", "0011_protect_identity_history") in REQUIRED_MIGRATIONS
    assert ("messaging", "0006_threadparticipant_principal_attribution") in REQUIRED_MIGRATIONS
    assert ("ai_app", "0015_ai_request_scope_privacy") in REQUIRED_MIGRATIONS
    assert ("printing", "0005_print_job_delivery_lease") in REQUIRED_MIGRATIONS
    assert ("printing", "0004_printjob_unique_open_source") in LEGACY_TENANT_ANCHORS
    assert not requires_safeguarding_cutover(set())  # brand-new schema has no old process/data
    assert requires_safeguarding_cutover({next(iter(LEGACY_TENANT_ANCHORS))})
    assert requires_safeguarding_cutover({next(iter(REQUIRED_MIGRATIONS))})
    assert not requires_safeguarding_cutover(REQUIRED_MIGRATIONS)


def test_release_cutover_manifest_names_real_migrations():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    assert set(loader.disk_migrations) >= REQUIRED_MIGRATIONS


def test_final_migration_state_preserves_defaults_without_plaintext_database_defaults():
    state = MigrationLoader(None, ignore_no_migrations=True).project_state(
        [
            ("parents", "0009_encrypt_safeguarding_text"),
            ("students", "0010_encrypt_emergency_contacts"),
        ]
    )
    parent_notes = state.models["parents", "parentprofile"].get_field("notes")
    custody_notes = state.models["parents", "guardian"].get_field("custody_notes")
    contacts = state.models["students", "studentprofile"].get_field("emergency_contacts")

    assert parent_notes.default is NOT_PROVIDED
    assert custody_notes.default is NOT_PROVIDED
    assert contacts.default is list
    assert all(
        field.null is False and field.db_default is NOT_PROVIDED
        for field in (parent_notes, custody_notes, contacts)
    )


def test_migrate_entrypoint_refuses_pending_cutover_without_ack(tmp_path):
    fake_python = tmp_path / "python"
    fake_python.write_text(
        '#!/bin/sh\ncase "$*" in\n  *core.migration_gate*) exit 1 ;;\nesac\nexit 99\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "STARFORGE_IMAGE_REVISION": "a" * 40,
        "STARFORGE_RELEASE_REVISION": "a" * 40,
        "STARFORGE_MAINTENANCE_CUTOVER": "0",
    }

    result = subprocess.run(
        ["bash", str(ROOT / "docker/entrypoint.sh"), "migrate"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 78
    assert "host-issued cutover evidence" in result.stderr


def test_migrate_entrypoint_runs_both_scopes_with_explicit_ack(tmp_path):
    calls = tmp_path / "calls"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$CUTOVER_TEST_CALLS"\n'
        'case "$*" in\n'
        "  *check_safeguarding_encryption_cutover*) echo required ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "STARFORGE_IMAGE_REVISION": "a" * 40,
        "STARFORGE_RELEASE_REVISION": "a" * 40,
        "STARFORGE_MAINTENANCE_CUTOVER": "a" * 40,
        "CUTOVER_TEST_CALLS": str(calls),
    }

    result = subprocess.run(
        ["bash", str(ROOT / "docker/entrypoint.sh"), "migrate"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    recorded = calls.read_text(encoding="utf-8")
    assert "migrate_schemas --shared" in recorded
    assert "migrate_schemas --tenant" in recorded


def test_migrate_entrypoint_rejects_ack_from_a_different_image(tmp_path):
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *core.migration_gate*) exit 1 ;;\n"
        "  *migrate_schemas*) exit 99 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "STARFORGE_IMAGE_REVISION": "b" * 40,
        "STARFORGE_RELEASE_REVISION": "a" * 40,
        "STARFORGE_MAINTENANCE_CUTOVER": "a" * 40,
    }

    result = subprocess.run(
        ["bash", str(ROOT / "docker/entrypoint.sh"), "migrate"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 78
    assert "does not match the immutable image" in result.stderr


def test_deploy_quiesces_apps_before_backup_and_migration():
    script = (ROOT / "scripts/deploy_production.sh").read_text(encoding="utf-8")
    stop = script.index("drain_project_applications")
    backup = script.index('"$release_dir/scripts/backup_production.sh"', stop)
    migrate = script.index("  apply_candidate_migrations", backup)

    assert stop < backup < migrate
    assert 'MAINTENANCE_CUTOVER_ACK" == "$sha"' in script
    assert "capture_broker_depth" in script
    assert "schema_change_started=1" in script
    assert "Do not start the old image against the migrated schema" in script
    assert "label=com.docker.compose.project=${project_name}" in script
    assert 'if ! is_stateful_service "$service"' in script


def test_deploy_requires_exact_remote_revision_and_has_no_ci_bypass():
    script = (ROOT / "scripts/deploy_production.sh").read_text(encoding="utf-8")

    assert '[[ "$revision" =~ ^[0-9a-f]{40}$ ]]' in script
    assert "merge-base --is-ancestor" in script
    assert "ALLOW_UNVERIFIED_CI" not in script
    assert "STARFORGE_IMAGE_REVISION" in script
    assert "STARFORGE_BOOTSTRAP_DEPLOY_BLOB" in script
    assert 'hash-object "${BASH_SOURCE[0]}"' in script
    assert "release-helpers.sha256" in script


def test_production_migration_entrypoint_always_requires_host_evidence():
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")

    assert "core.migration_gate" in entrypoint
    assert "/run/secrets/migration-cutover.evidence" in entrypoint
    assert "check_empty_production_database --token" in entrypoint
    assert "check_safeguarding_encryption_cutover --token" not in entrypoint


def test_launcher_executes_only_detached_exact_revision_bytes():
    launcher = (ROOT / "scripts/launch_production_deploy.sh").read_text(encoding="utf-8")

    assert 'worktree add --detach "$release_tree" "$sha"' in launcher
    assert 'rev-parse "${sha}:scripts/deploy_production.sh"' in launcher
    assert 'hash-object "$deploy_script"' in launcher
    assert 'STARFORGE_BOOTSTRAP_DEPLOY_BLOB="$expected_blob"' in launcher


def test_deploy_has_two_phase_human_review_and_idempotent_apply_recovery():
    script = (ROOT / "scripts/deploy_production.sh").read_text(encoding="utf-8")

    dry_run = script.index("generate_backfill_review")
    review_gate = script.index('REVIEWED_BACKFILL_SHA256" != "$review_digest"', dry_run)
    apply = script.index("applied-notification-principals.json", review_gate)
    start = script.index('echo "Starting release', apply)

    assert dry_run < review_gate < apply < start
    assert "phase=review_pending" in script
    assert "phase=apply_started" in script
    assert "phase=backfills_applied" in script
    assert "STARFORGE_RESUME_FAILED_CUTOVER" in script
    assert "check_ai_attribution --fail-on-expired-content" in script
    assert "ai_sha256=" in script


def test_pre_schema_failure_never_creates_a_post_schema_failure_marker():
    script = (ROOT / "scripts/deploy_production.sh").read_text(encoding="utf-8")
    trap_start = script.index("on_exit()")
    trap_end = script.index("trap on_exit EXIT", trap_start)
    trap = script[trap_start:trap_end]

    schema_branch = trap.index('if [[ "$schema_change_started" == "0" ]]')
    optional_restart = trap.index('if [[ "${#previous_app_containers[@]}" -gt 0 ]]', schema_branch)
    post_schema_branch = trap.index("    else\n      quiesce_project_applications", optional_restart)
    failure_marker = trap.index("write_cutover_failure_marker", post_schema_branch)

    assert schema_branch < optional_restart < post_schema_branch < failure_marker


def test_failed_cutover_marker_takes_precedence_over_review_marker():
    script = (ROOT / "scripts/deploy_production.sh").read_text(encoding="utf-8")

    assert script.index('if [[ -e "$failure_marker" ]]') < script.index('elif [[ -e "$review_marker" ]]')


def test_resume_reasserts_quiescence_before_candidate_one_off_commands():
    script = (ROOT / "scripts/deploy_production.sh").read_text(encoding="utf-8")
    resume_guard = script.index('if [[ "$resume_review" == "1" || "$resume_migration" == "1" ]]')
    quiesce = script.index("quiesce_project_applications 0", resume_guard)
    production_check = script.index("python manage.py check --deploy", quiesce)

    assert resume_guard < quiesce < production_check


def test_candidate_is_persisted_before_schema_change_to_block_old_compose_restart():
    script = (ROOT / "scripts/deploy_production.sh").read_text(encoding="utf-8")
    migration_function = script.index("apply_candidate_migrations()")
    persistent_pin = script.index("  pin_persistent_candidate_image", migration_function)
    schema_boundary = script.index("  schema_change_started=1", persistent_pin)
    migrate = script.index('"${compose[@]}" --profile tools run --rm --no-deps -T migrate', schema_boundary)

    assert persistent_pin < schema_boundary < migrate
    assert 'line.startswith("APP_IMAGE=")' in script
    assert "os.replace(temporary_name, path)" in script


def test_release_preflight_is_read_only_and_pseudonymizes_schemas():
    command = (ROOT / "apps/tenancy/management/commands/check_release_migration_preflight.py").read_text(
        encoding="utf-8"
    )

    assert 'cursor.execute("SET TRANSACTION READ ONLY")' in command
    assert '"schema_ref": _schema_ref(schema_name)' in command
    assert '"schema": schema_name' not in command
    assert "--fail-on-blocked" in command
