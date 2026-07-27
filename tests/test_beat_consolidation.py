"""Beat consolidation completeness (D4-LF-4).

The single source of truth for periodic work is ``settings.CELERY_BEAT_SCHEDULE``
(DatabaseScheduler ingests it at beat startup). These tests enforce three
invariants so a beat tick never references a task no worker registered (the
Day-1 blocker that left trial-expiry / OTP-purge dead on arrival):

1. Every ``CELERY_BEAT_SCHEDULE`` entry's ``task`` is registered in ``app.tasks``
   via the SAME autodiscovery path a real ``celery -A config worker`` takes.
2. Every task in the consolidated Day-1..4 table (``CANONICAL_BEAT_TASKS``) that
   is registered appears in ``CELERY_BEAT_SCHEDULE`` (no periodic task is left
   un-scheduled).
3. No module outside ``config/settings`` defines an ad-hoc periodic schedule
   (one table to rule them all).

``CANONICAL_BEAT_TASKS`` is the authoritative table published to WORKLOG. A few
rows depend on lanes that merge before Lane F (reports B); those task modules are
already imported by the autodiscovery aggregator, so once the lane lands the task
registers and rows 2 tightens automatically. Until then the row is reported as a
pending integration (xfail) rather than a hard failure, so this test is green in
the Lane-F worktree and strict after merge.
"""

from __future__ import annotations

import pytest
from django.conf import settings

from config.celery import app

# (beat-key, dotted task path) — the consolidated Day-1..4 schedule. The dotted
# path is the registered task name (module-qualified) the worker resolves.
CANONICAL_BEAT_TASKS: dict[str, str] = {
    # Day 1
    "purge-expired-otps": "celery_tasks.cleanup_tasks.purge_expired_otps",
    "deactivate-expired-trials": "celery_tasks.tenancy_tasks.deactivate_expired_trials",
    # Day 2
    "mark-absent-after-lesson": "celery_tasks.attendance_tasks.mark_absent_after_lesson",
    "send-lesson-reminders": "celery_tasks.schedule_tasks.send_lesson_reminders",
    "archive-completed-terms": "celery_tasks.schedule_tasks.archive_completed_terms",
    "send-due-soon-reminders": "celery_tasks.assignment_tasks.send_due_soon_reminders",
    # Day 3
    "late-payment-reminders": "celery_tasks.finance_tasks.late_payment_reminders",
    "cleanup-old-audit-logs": "celery_tasks.audit_tasks.cleanup_old_audit_logs",
    "run-nightly-metering": "celery_tasks.billing_tasks.run_nightly_metering",
    # Day 4 — Lane F additions
    "flush-expired-jwt-blacklist": "celery_tasks.cleanup_tasks.flush_expired_jwt_blacklist",
    # Day 4 — Lane B reports (registered once Lane B lands; aggregator already
    # imports celery_tasks.report_tasks, so registration follows the build).
    "run-due-report-schedules": "celery_tasks.report_tasks.run_due_report_schedules",
    "nightly-platform-aggregation": "celery_tasks.report_tasks.nightly_platform_aggregation",
}

# Tasks whose CELERY_BEAT_SCHEDULE entry is delivered by Lane F's
# integration_needed block (config/settings/base.py is off-limits to Lane F).
# They are registered as tasks but only appear in the *schedule* after the
# orchestrator applies the consolidated table — so invariant 2b treats them as
# pending until then (the test tightens automatically once applied).
PENDING_BEAT_INTEGRATION = {
    "celery_tasks.cleanup_tasks.flush_expired_jwt_blacklist",
    "celery_tasks.report_tasks.run_due_report_schedules",
    "celery_tasks.report_tasks.nightly_platform_aggregation",
}


@pytest.fixture(scope="module", autouse=True)
def _finalize_worker():
    """Register every task the way `celery -A config worker` does at init."""
    app.loader.import_default_modules()
    app.finalize()


def test_every_beat_entry_references_a_registered_task():
    """Invariant 1: no beat entry points at an unregistered task."""
    for key, entry in settings.CELERY_BEAT_SCHEDULE.items():
        task = entry["task"]
        assert task in app.tasks, f"beat entry {key!r} references unregistered task {task!r}"


def test_canonical_periodic_tasks_are_registered():
    """Invariant 2a: every consolidated task is importable/registered (or, for a
    lane that merges before F, is flagged as a pending integration)."""
    missing = [t for t in set(CANONICAL_BEAT_TASKS.values()) if t not in app.tasks]
    pending = {
        "celery_tasks.report_tasks.run_due_report_schedules",
        "celery_tasks.report_tasks.nightly_platform_aggregation",
    }
    hard_missing = [t for t in missing if t not in pending]
    assert not hard_missing, f"consolidated tasks not registered: {hard_missing}"
    if any(t in missing for t in pending):
        pytest.xfail(
            "Lane B report tasks (run_due_report_schedules / nightly_platform_aggregation) not merged yet"
        )


def test_registered_canonical_tasks_are_scheduled():
    """Invariant 2b: every registered consolidated task is in CELERY_BEAT_SCHEDULE.

    Tasks delivered by Lane F's own integration_needed beat block (config/settings
    is off-limits) are exempt until the orchestrator applies that block; an exempt
    task that is NOT yet scheduled marks the test xfail so it tightens on merge.
    """
    scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    awaiting_integration: list[str] = []
    for key, task in CANONICAL_BEAT_TASKS.items():
        if task not in app.tasks:
            continue  # not-yet-merged lane task — covered by test 2a's xfail
        if task in scheduled:
            continue
        if task in PENDING_BEAT_INTEGRATION:
            awaiting_integration.append(task)
            continue
        raise AssertionError(
            f"registered task {task!r} (beat key {key!r}) is missing from CELERY_BEAT_SCHEDULE"
        )
    if awaiting_integration:
        pytest.xfail(
            "Lane F consolidated beat block not applied yet (integration_needed): "
            + ", ".join(sorted(awaiting_integration))
        )


def test_no_adhoc_periodic_schedule_outside_settings():
    """Invariant 3: only config/settings declares periodic schedules."""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for sub in ("apps", "celery_tasks", "core", "infrastructure"):
        for path in (repo / sub).rglob("*.py"):
            if "test" in path.name or "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8")
            if "add_periodic_task" in text or "beat_schedule = " in text:
                offenders.append(str(path.relative_to(repo)))
    assert not offenders, f"ad-hoc periodic schedules found outside settings: {offenders}"
