"""Campaign beat tasks (F10-1 dynamic send date). Fan-out per tenant; the body lives
in apps.campaigns.services.dispatch_due_campaigns. Emit-only dispatcher — the SMS I/O
happens inside send_campaign, never in a request handler."""

from __future__ import annotations

from config.celery import app


def _active_schemas():
    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center

    # Exclude the public Center: campaign tables are TENANT_APPS-only and do not exist in
    # the public schema (mirrors schedule_tasks._active_schemas).
    return list(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .values_list("schema_name", flat=True)
    )


@app.task
def dispatch_scheduled_campaigns() -> int:
    """Public dispatcher: fan out the due-campaign sweep to each active Center."""
    schemas = _active_schemas()
    for schema in schemas:
        dispatch_scheduled_campaigns_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task
def dispatch_scheduled_campaigns_for_schema() -> int:
    from apps.campaigns.services import dispatch_due_campaigns

    return dispatch_due_campaigns()
