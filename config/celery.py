"""Celery app entrypoint.

Uses tenant-schemas-celery so every task body runs under the correct
tenant schema. The `schema_name` kwarg is passed automatically by the
client wrapper; calling `task.delay(..., _schema_name="acme")` activates
the schema for the task's lifetime.
"""

import os

from tenant_schemas_celery.app import CeleryApp

# Fail-safe default (matches wsgi.py): assume production when unset. Dev + Docker
# set DJANGO_SETTINGS_MODULE explicitly.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

# SchemaHeaderTask lets `.delay(..., _schema_name="acme")` work correctly: it
# lifts the kwarg into task headers, where tenant-schemas-celery reads it to
# activate the schema (otherwise it leaks into the task signature).
app = CeleryApp("starforge", task_cls="core.celery_base:SchemaHeaderTask")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["celery_tasks"])

# D4-LF-5: DLQ on exhausted retries (Redis list `starforge:dlq`) + per-task
# duration logging. Idempotent (dispatch_uid); handlers live in celery_tasks.
from celery_tasks.observability import connect_celery_observability  # noqa: E402

connect_celery_observability(app)


@app.task(bind=True)
def debug_task(self):  # pragma: no cover
    print(f"Request: {self.request!r}")
