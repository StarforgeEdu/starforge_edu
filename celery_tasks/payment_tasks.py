"""Payments async tasks (D3-B-9, D3-B-10).

- ``fiscalize_payment`` — post-payment Soliq fiscalization. Idempotent (an
  existing CONFIRMED ``FiscalReceipt`` short-circuits in the service body),
  retries ≤3 with exponential backoff. Enqueued with ``_schema_name`` from the
  payment-completion chokepoint.
- ``generate_receipt_pdf`` — renders the fiscal receipt to PDF (weasyprint LAZY,
  in the service body) → S3 → a server-derived key is stored separately so
  ``GET /payments/{id}/receipt/`` can sign it (TD-14). PDF render skips where the
  native lib is absent (test mirrors the academics transcript skip).

No weasyprint/Soliq call ever happens in a request handler (DoD #9).
"""

from __future__ import annotations

import requests
from django.conf import settings

from config.celery import app


def _fiscalization_enabled() -> bool:
    return bool(getattr(settings, "FISCALIZATION_ENABLED", True))


@app.task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=3,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def fiscalize_payment(self, payment_id: int) -> str | None:
    if not _fiscalization_enabled():
        return None

    from apps.payments.services import fiscalize_payment_body, mark_fiscal_failed

    try:
        return fiscalize_payment_body(payment_id)
    except requests.RequestException as exc:
        # Release the SUBMITTED lease before autoretry. Otherwise the retry sees
        # its own fresh lease, returns early, and falsely acknowledges unfinished
        # fiscal work until the periodic stale-lease sweep eventually notices it.
        mark_fiscal_failed(payment_id, exc)
        raise  # autoretry_for handles backoff/retry
    except Exception as exc:
        mark_fiscal_failed(payment_id, exc)
        raise self.retry(exc=exc) from exc


@app.task
def reconcile_fiscal_receipts() -> int:
    """Fan out durable fiscal-outbox recovery to every active tenant."""
    if not _fiscalization_enabled():
        return 0

    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center

    schemas = list(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .values_list("schema_name", flat=True)
    )
    for schema in schemas:
        reconcile_fiscal_receipts_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task(acks_late=True, reject_on_worker_lost=True)
def reconcile_fiscal_receipts_for_schema() -> int:
    """Re-enqueue pending/failed or stale-claimed fiscal receipts.

    The marker is created transactionally for every new completion. Seed markers
    for completed rows predating that invariant as well, so a deployment upgrade
    cannot strand historical receipts merely because no outbox row existed yet.
    """
    if not _fiscalization_enabled():
        return 0

    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    from apps.payments.models import FiscalReceipt, Payment
    from core.utils import current_schema

    stale_before = timezone.now() - timedelta(minutes=15)
    missing_payment_ids = list(
        Payment.objects.filter(
            status__in=(Payment.Status.COMPLETED, Payment.Status.REFUNDED),
            fiscal_receipt__isnull=True,
        )
        .order_by("paid_at", "id")
        .values_list("id", flat=True)[:500]
    )
    FiscalReceipt.objects.bulk_create(
        [FiscalReceipt(payment_id=payment_id) for payment_id in missing_payment_ids],
        ignore_conflicts=True,
    )
    ids = list(
        FiscalReceipt.objects.filter(
            Q(status__in=(FiscalReceipt.Status.PENDING, FiscalReceipt.Status.FAILED))
            | Q(status=FiscalReceipt.Status.SUBMITTED, submitted_at__lt=stale_before),
            payment__status__in=(Payment.Status.COMPLETED, Payment.Status.REFUNDED),
        )
        .order_by("created_at")
        .values_list("payment_id", flat=True)[:500]
    )
    schema = current_schema()
    for payment_id in ids:
        fiscalize_payment.delay(payment_id, _schema_name=schema)
    return len(ids)


@app.task(bind=True, max_retries=3, retry_backoff=True)
def generate_receipt_pdf(self, payment_id: int) -> str | None:
    from apps.payments.services import generate_receipt_pdf_body

    try:
        return generate_receipt_pdf_body(payment_id)
    except Exception as exc:
        raise self.retry(exc=exc) from exc


# WebhookEvent retention (R6/CONF3). The event ledger is a replay-dedupe + audit table
# that grows one row per authenticated provider webhook (invalid signatures are never
# persisted), and this beat task bounds long-term growth past the retention window.
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
