"""Campaign dispatch and delivery tasks (F10-1 dynamic send date)."""

from __future__ import annotations

from django.conf import settings

from config.celery import app


def _sms_enabled() -> bool:
    return bool(getattr(settings, "SMS_ENABLED", True))


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
    if not _sms_enabled():
        return 0
    schemas = _active_schemas()
    for schema in schemas:
        dispatch_scheduled_campaigns_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task
def dispatch_scheduled_campaigns_for_schema() -> int:
    if not _sms_enabled():
        return 0
    from apps.campaigns.services import dispatch_due_campaigns

    return dispatch_due_campaigns()


@app.task(
    bind=True,
    max_retries=3,
    retry_backoff=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def deliver_campaign(self, campaign_id: int, claim_token: str) -> str | None:
    """Run provider I/O in a worker under a durable campaign lease."""
    if not _sms_enabled():
        return "disabled"
    from apps.campaigns.services import (
        process_campaign_delivery,
        record_campaign_delivery_error,
    )

    try:
        return process_campaign_delivery(campaign_id=campaign_id, claim_token=claim_token)
    except Exception as exc:
        record_campaign_delivery_error(
            campaign_id=campaign_id,
            claim_token=claim_token,
            error=exc,
        )
        raise self.retry(exc=exc) from exc
