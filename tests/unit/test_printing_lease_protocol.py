"""Database-free release guards for physical-print lease operations."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings

from apps.printing.services import print_agent_lease_seconds

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("value", [True, 59, 3601, "600"])
def test_print_lease_duration_fails_closed_outside_reviewed_bounds(value):
    with (
        override_settings(PRINT_AGENT_LEASE_SECONDS=value),
        pytest.raises(ImproperlyConfigured),
    ):
        print_agent_lease_seconds()


def test_print_lease_migration_quarantines_legacy_inflight_state():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    state = loader.project_state([("printing", "0005_print_job_delivery_lease")])
    job = state.models["printing", "printjob"]
    status = job.get_field("status")

    assert "reconciliation_required" in {value for value, _label in status.choices}
    assert job.get_field("lease_id").unique is True
    assert job.get_field("lease_expires_at").null is True
    assert ("printing", "printjobreconciliation") in state.models

    migration = loader.disk_migrations[("printing", "0005_print_job_delivery_lease")]
    run_python = [
        operation for operation in migration.operations if operation.__class__.__name__ == "RunPython"
    ]
    assert len(run_python) == 1
    source = (ROOT / "apps/printing/migrations/0005_print_job_delivery_lease.py").read_text(encoding="utf-8")
    assert 'status="reconciliation_required"' in source
    assert 'status="queued"' not in source


def test_stale_print_sweep_is_registered_bounded_and_maintenance_routed(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["quarantine-stale-print-leases"]
    assert entry["task"] == "celery_tasks.print_tasks.quarantine_stale_print_leases"
    assert entry["schedule"] == 60.0
    assert entry["options"] == {"queue": "maintenance", "expires": 55}
    assert settings.CELERY_TASK_ROUTES["celery_tasks.print_tasks.quarantine_stale_print_leases*"] == {
        "queue": "maintenance"
    }
    assert 1 <= settings.PRINT_STALE_LEASE_SWEEP_BATCH_SIZE <= 1000


def test_print_reconciliation_runbook_forbids_inferred_replay():
    runbook = (ROOT / "docs/runbooks/print-agent-reconciliation.md").read_text(encoding="utf-8")
    for required in (
        "non-rolling protocol migration",
        "never treats silence as proof",
        "confirmed_printed",
        "confirmed_not_printed",
        "abandoned_unknown",
        "never requeues",
        "Idempotency-Key",
        "check_print_reconciliation",
    ):
        assert required in runbook
