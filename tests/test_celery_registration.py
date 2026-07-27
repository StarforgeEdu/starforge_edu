"""Worker-side Celery task registration guard.

Regression for the Day-1 blocker: ``autodiscover_tasks(["celery_tasks"])``
imports only ``celery_tasks.tasks``, which did not exist, so every beat tick
produced 'Received unregistered task' and trial expiry / OTP purge never ran
in any deployed environment. Importing the task module directly in a test
(as test_plumbing.py does) registers it as a side effect and masks the bug —
this test exercises the same import path a real ``celery -A config worker``
takes at init.
"""

from django.conf import settings

from config.celery import app


def test_beat_tasks_registered_via_autodiscovery():
    app.loader.import_default_modules()  # what `celery -A config worker` runs at init
    app.finalize()
    for entry in settings.CELERY_BEAT_SCHEDULE.values():
        assert entry["task"] in app.tasks, f"beat references unregistered task {entry['task']!r}"
    assert "celery_tasks.tenancy_tasks.deactivate_expired_trials" in app.tasks
    assert "celery_tasks.cleanup_tasks.purge_expired_otps" in app.tasks


def test_report_schedule_scan_is_clock_aligned_crontab():
    """The report-schedule scan must use a clock-aligned crontab (:00), not a
    fixed interval — schedule_is_due requires an exact hour match, so a drifting
    interval could skip an hour bucket (and its due schedules) after a restart."""
    from celery.schedules import crontab

    entry = settings.CELERY_BEAT_SCHEDULE["run-due-report-schedules"]
    assert isinstance(entry["schedule"], crontab)
    assert entry["schedule"].minute == {0}  # fires at the top of every hour
