"""Finance beat/async tasks (D3-A-7, D3-A-8).

- `generate_statement_pdf` drives one durable statement-export row through
  rendering (weasyprint, lazy import in the service) and private upload to its
  deterministic `{schema}/documents/` key (TD-14). Per-tenant (enqueued with
  `_schema_name`), retries <=3 with backoff. No weasyprint/S3 call ever happens
  in a request handler (DoD #9).
- `late_payment_reminders` is the daily beat task: it fans out per active Center,
  scans overdue invoices, and emits `payment_reminder` once per invoice per
  `CenterSettings.payment_reminder_interval_days` (dedupe in the service body).
- `refresh_fx_rates` (mock-first) caches the per-tenant CBU UZS->USD rate that
  `issue_invoice` snapshots; real CBU fetch flips on when the source is live.
"""

from __future__ import annotations

from config.celery import app


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
    export_id: str,
) -> str | None:
    """Drive one durable statement-export row to a terminal state."""
    from apps.finance.services import (
        build_statement_export,
        mark_statement_export_failed,
        reset_statement_export_for_retry,
    )
    from core.exceptions import ConflictException

    try:
        return build_statement_export(export_id)
    except Exception as exc:
        if isinstance(exc, ConflictException) and exc.code == "statement_export_in_progress":
            raise self.retry(
                exc=RuntimeError("Finance statement generation is already in progress."),
                countdown=5,
            ) from None
        safe_exc = RuntimeError("Finance statement generation failed.")
        if self.request.retries >= self.max_retries:
            mark_statement_export_failed(export_id, exc)
            raise safe_exc from None
        reset_statement_export_for_retry(export_id)
        raise self.retry(exc=safe_exc) from None


@app.task
def maintain_statement_exports() -> int:
    """Periodic public dispatcher for retention and lost-publish recovery."""

    schemas = _active_schemas()
    for schema in schemas:
        maintain_statement_exports_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task
def maintain_statement_exports_for_schema() -> dict[str, int]:
    from apps.finance.services import (
        expire_statement_exports,
        statement_exports_needing_redelivery,
    )
    from core.utils import current_schema

    cleaned = expire_statement_exports()
    recovered = statement_exports_needing_redelivery()
    schema = current_schema()
    for export_id in recovered:
        generate_statement_pdf.delay(str(export_id), _schema_name=schema)
    return {"cleaned": cleaned, "redelivered": len(recovered)}


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
