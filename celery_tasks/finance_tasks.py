"""Finance beat/async tasks (D3-A-7, D3-A-8).

- `generate_statement_pdf` renders a statement-of-account to PDF (weasyprint,
  lazy import in the service) and uploads it to `{schema}/documents/` (TD-14),
  caching the task-id -> S3 key map so the result endpoint can sign it. Per-tenant
  (enqueued with `_schema_name`), retries <=3 with backoff. No weasyprint/S3 call
  ever happens in a request handler (DoD #9).
- `late_payment_reminders` is the daily beat task: it fans out per active Center,
  scans overdue invoices, and emits `payment_reminder` once per invoice per
  `CenterSettings.payment_reminder_interval_days` (dedupe in the service body).
- `refresh_fx_rates` (mock-first) caches the per-tenant CBU UZS->USD rate that
  `issue_invoice` snapshots; real CBU fetch flips on when the source is live.
"""

from __future__ import annotations

import re

from config.celery import app

_STATEMENT_KEY_SUFFIX_RE = re.compile(r"^statement_[1-9][0-9]*_[0-9]{14}_[0-9a-f]{32}\.pdf$")


def _active_schemas() -> list[str]:
    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center

    return list(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .values_list("schema_name", flat=True)
    )


@app.task(bind=True, max_retries=3, retry_backoff=True)
def generate_statement_pdf(
    self,
    student_id: int,
    *,
    locale: str = "en",
    requested_by_id: int,
) -> str | None:
    """Render + upload one student's statement; cache the task-id -> key map."""
    from django.core.cache import cache

    from apps.finance.services import generate_statement_artifact
    from core.utils import current_schema

    schema = current_schema()
    cache_key = f"finance:statement:{schema}:{self.request.id}"
    cached_key = _trusted_cached_statement(
        cache.get(cache_key),
        schema=schema,
        student_id=student_id,
        requested_by_id=requested_by_id,
    )
    if cached_key is not None:
        return cached_key
    try:
        artifact = generate_statement_artifact(
            student_id,
            locale=locale,
            requested_by_id=requested_by_id,
        )
        cache.set(
            cache_key,
            {
                "key": artifact.key,
                "requested_by_id": requested_by_id,
                "student_id": student_id,
                "invoice_ids": list(artifact.invoice_ids),
            },
            timeout=3600,
        )
        return artifact.key
    except Exception:
        # Provider/render exceptions can contain filesystem paths, signed URLs,
        # or document data. Celery/DLQ receives only a stable safe error while
        # the task's configured retry budget handles transient failures.
        raise self.retry(exc=RuntimeError("Finance statement generation failed.")) from None


def _trusted_cached_statement(
    value: object,
    *,
    schema: str,
    student_id: int,
    requested_by_id: int,
) -> str | None:
    """Accept only the exact cache record this task writes after upload.

    A late-ack redelivery keeps its Celery task id. Reusing the completed cache
    record prevents a second expensive render and random-key storage orphan.
    """

    if not isinstance(value, dict):
        return None
    if value.get("student_id") != student_id or value.get("requested_by_id") != requested_by_id:
        return None
    invoice_ids = value.get("invoice_ids")
    if (
        not isinstance(invoice_ids, list)
        or len(invoice_ids) > 5_000
        or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in invoice_ids)
        or invoice_ids != sorted(set(invoice_ids))
    ):
        return None
    key = value.get("key")
    if not isinstance(key, str):
        return None
    prefix = f"{schema}/documents/"
    if not key.startswith(prefix) or not _STATEMENT_KEY_SUFFIX_RE.fullmatch(key.removeprefix(prefix)):
        return None
    expected_prefix = f"statement_{student_id}_"
    if not key.removeprefix(prefix).startswith(expected_prefix):
        return None
    return key


@app.task
def late_payment_reminders() -> int:
    """Daily public dispatcher; one failing center cannot starve later tenants."""
    schemas = _active_schemas()
    for schema in schemas:
        late_payment_reminders_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task(bind=True, max_retries=3, retry_backoff=True)
def late_payment_reminders_for_schema(self) -> int:
    from apps.finance.services import emit_payment_reminders

    try:
        return emit_payment_reminders()
    except Exception as exc:
        raise self.retry(exc=exc) from exc


@app.task
def refresh_fx_rates() -> int:
    """Daily public dispatcher for tenant-local FX cache refreshes."""
    schemas = _active_schemas()
    for schema in schemas:
        refresh_fx_rate_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task(bind=True, max_retries=3, retry_backoff=True)
def refresh_fx_rate_for_schema(self) -> str | None:
    """Cache the per-tenant CBU UZS->USD rate consumed by `issue_invoice`.

    Development/test may set `FINANCE_FX_USE_MOCK` to write a deterministic
    placeholder. Production forces it off and fetches the CBU JSON feed when the
    tenant source is "cbu". Per-tenant; fan out over active Centers.
    """

    from django.conf import settings
    from django.core.cache import cache

    from apps.org.selectors import get_center_settings
    from core.utils import current_schema

    try:
        cs = get_center_settings()
        if (cs.fx_source or "cbu") != "cbu":
            return None
        # This setting is explicit in every environment; never silently fall back
        # to a fabricated financial rate when configuration drifts.
        use_mock = settings.FINANCE_FX_USE_MOCK
        rate = _mock_cbu_rate() if use_mock else _live_cbu_rate()
        if rate is not None:
            cache.set(f"finance:fx_rate_usd:{current_schema()}", str(rate), timeout=24 * 3600)
        return str(rate) if rate is not None else None
    except Exception as exc:
        raise self.retry(exc=exc) from exc


def _mock_cbu_rate():
    from decimal import Decimal

    return Decimal("12500.0000")  # deterministic dev/test UZS per USD


def _live_cbu_rate():
    """Fetch a small strict CBU UZS->USD response without following redirects."""
    import requests

    from apps.finance.services import normalize_fx_rate
    from infrastructure.http_client import request_json_limited

    data = request_json_limited(
        requests.get,
        "https://cbu.uz/uz/arkhiv-kursov-valyut/json/USD/",
        allowed_hosts=("cbu.uz",),
        timeout=(3.05, 10.0),
        max_response_bytes=64 * 1024,
    )
    if isinstance(data, list) and data and isinstance(data[0], dict):
        row = data[0]
        if row.get("Ccy") != "USD":
            raise ValueError("CBU rate response is not the requested USD currency")
        rate = normalize_fx_rate(row.get("Rate"))
        if rate is not None:
            return rate
    raise ValueError("CBU rate response does not contain a valid USD rate")
