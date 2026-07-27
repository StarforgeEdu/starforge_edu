"""Payments async tasks (D3-B-9, D3-B-10).

- ``fiscalize_payment`` — post-payment Soliq fiscalization. Idempotent (an
  existing CONFIRMED ``FiscalReceipt`` short-circuits in the service body),
  retries ≤3 with exponential backoff. Enqueued with ``_schema_name`` from the
  payment-completion chokepoint.
- ``generate_receipt_pdf`` — renders the fiscal receipt to PDF (weasyprint LAZY,
  in the service body) → S3 → the key is stored on the receipt payload so
  ``GET /payments/{id}/receipt/`` can sign it (TD-14). PDF render skips where the
  native lib is absent (test mirrors the academics transcript skip).

No weasyprint/Soliq call ever happens in a request handler (DoD #9).
"""

from __future__ import annotations

import requests

from config.celery import app


@app.task(
    bind=True,
    max_retries=3,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def fiscalize_payment(self, payment_id: int) -> str | None:
    from apps.payments.services import fiscalize_payment_body, mark_fiscal_failed

    try:
        return fiscalize_payment_body(payment_id)
    except requests.RequestException:
        raise  # autoretry_for handles backoff/retry
    except Exception as exc:
        mark_fiscal_failed(payment_id, exc)
        raise self.retry(exc=exc) from exc


@app.task(bind=True, max_retries=3, retry_backoff=True)
def generate_receipt_pdf(self, payment_id: int) -> str | None:
    from apps.payments.services import generate_receipt_pdf_body

    try:
        return generate_receipt_pdf_body(payment_id)
    except Exception as exc:
        raise self.retry(exc=exc) from exc


# WebhookEvent retention (R6/CONF3). The event ledger is a replay-dedupe + audit table
# that grows one row per inbound webhook; the per-IP invalid-webhook throttle caps a burst,
# and this beat task bounds LONG-TERM growth by pruning events past the retention window.
# Safe: providers retry within hours/days, never months, so a pruned old event can't be a
# live replay; and the recent audit trail (well beyond any provider retry window) is kept.
WEBHOOK_RETENTION_DAYS = 90


@app.task
def prune_webhook_events() -> int:
    """Public dispatcher: fan the WebhookEvent retention sweep out to each active Center."""
    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center

    # WebhookEvent is a TENANT model (recorded inside schema_context); exclude the public
    # Center where the table doesn't exist (mirrors cleanup_old_audit_logs).
    schemas = list(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .values_list("schema_name", flat=True)
    )
    for schema in schemas:
        prune_webhook_events_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task
def prune_webhook_events_for_schema() -> int:
    """Per-tenant sweep: delete WebhookEvent rows older than the retention window."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.payments.models import WebhookEvent

    cutoff = timezone.now() - timedelta(days=WEBHOOK_RETENTION_DAYS)
    deleted, _detail = WebhookEvent.objects.filter(created_at__lt=cutoff).delete()
    return int(deleted)
