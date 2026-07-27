"""AI signal receivers (D4-LA-7).

Wires the two emit-only Day-2 signals to the AI Celery tasks:

- ``apps.assignments.signals.ai_feedback_requested`` → ``run_assignment_feedback``
- ``apps.content.signals.file_upload_confirmed``     → ``run_content_summary``

Both enqueue on ``transaction.on_commit`` via the task (the emitter already
fires inside ``on_commit``, so the row is committed) and pass ``_schema_name`` so
the worker activates the right tenant schema. Idempotency is the task's job: it
reserves budget under the ``AIRequest`` idempotency key, so a duplicate signal
delivery resolves to the same row instead of a second job.

``dispatch_uid`` + ``weak=False`` are mandatory — without them a double
registration double-enqueues, or a GC'd local receiver silently does nothing
(both bit Day-3, see WORKLOG).
"""

from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.assignments.signals import ai_feedback_requested
from apps.content.signals import file_upload_confirmed

logger = logging.getLogger("starforge.ai")


@receiver(ai_feedback_requested, dispatch_uid="ai.assignment_feedback", weak=False)
def on_ai_feedback_requested(sender, *, submission_id, requested_by=None, schema_name, **kwargs):
    from celery_tasks.ai_tasks import run_assignment_feedback

    run_assignment_feedback.delay(submission_id, requested_by=requested_by, _schema_name=schema_name)


@receiver(file_upload_confirmed, dispatch_uid="ai.content_summary", weak=False)
def on_file_upload_confirmed(sender, *, file_id, requested_by=None, schema_name, **kwargs):
    from celery_tasks.ai_tasks import run_content_summary

    run_content_summary.delay(file_id, requested_by=requested_by, _schema_name=schema_name)
