"""Aggregator imported by Celery autodiscovery (related_name="tasks").

config.celery's `app.autodiscover_tasks(["celery_tasks"])` imports exactly
`celery_tasks.tasks`; importing each module here registers its `@app.task`
functions with the worker. Keep the imports HERE rather than in
celery_tasks/__init__.py: the task modules import Django models at module
level (e.g. cleanup_tasks imports apps.users.models.OTP), so an eager package
__init__ would break any `import celery_tasks.<x>` that happens before
django.setup(); this module is only imported by Celery's
import_default_modules, which runs after the Django fixup.
"""

from celery_tasks import (  # noqa: F401
    academics_tasks,
    ai_tasks,
    assignment_tasks,
    attendance_tasks,
    audit_tasks,
    billing_tasks,
    campaign_tasks,
    cleanup_tasks,
    content_tasks,
    finance_tasks,
    notification_tasks,
    payment_tasks,
    print_tasks,
    report_tasks,
    schedule_tasks,
    tenancy_tasks,
)
