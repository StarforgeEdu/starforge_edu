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

from config.celery import app


@app.task(bind=True, max_retries=3, retry_backoff=True)
def generate_statement_pdf(self, student_id: int, *, locale: str = "en") -> str | None:
    """Render + upload one student's statement; cache the task-id -> key map."""
    from django.core.cache import cache

    from apps.finance.services import generate_statement
    from core.utils import current_schema

    key = generate_statement(student_id, locale=locale)
    cache.set(
        f"finance:statement:{current_schema()}:{self.request.id}",
        key,
        timeout=3600,
    )
    return key


@app.task
def late_payment_reminders() -> dict:
    """Daily beat: for every active Center, emit payment reminders for overdue
    invoices. Idempotent within a reminder interval (dedupe in the service)."""
    from django_tenants.utils import get_public_schema_name, schema_context

    from apps.finance.services import emit_payment_reminders
    from apps.tenancy.models import Center

    # Exclude the public Center: finance tables are TENANT_APPS-only (absent in
    # public), so fanning the per-tenant body there raises ProgrammingError.
    results: dict[str, int] = {}
    for center in (
        Center.objects.filter(is_active=True).exclude(schema_name=get_public_schema_name()).iterator()
    ):
        with schema_context(center.schema_name):
            results[center.schema_name] = emit_payment_reminders()
    return results


@app.task(bind=True, max_retries=3, retry_backoff=True)
def refresh_fx_rates(self) -> dict:
    """Cache the per-tenant CBU UZS->USD rate consumed by `issue_invoice`.

    Mock-first (TD-2): with `FINANCE_FX_USE_MOCK` True (default) this writes a
    deterministic placeholder rate so USD totals are populated in dev/tests. The
    real branch fetches the CBU JSON feed with `requests` (lazy import) when the
    flag is off and the source is "cbu". Per-tenant; fan out over active Centers.
    """

    from django.conf import settings
    from django.core.cache import cache
    from django_tenants.utils import get_public_schema_name, schema_context

    from apps.org.selectors import get_center_settings
    from apps.tenancy.models import Center

    use_mock = getattr(settings, "FINANCE_FX_USE_MOCK", True)
    # Exclude the public Center: org/finance tables are TENANT_APPS-only.
    results: dict[str, str | None] = {}
    for center in (
        Center.objects.filter(is_active=True).exclude(schema_name=get_public_schema_name()).iterator()
    ):
        with schema_context(center.schema_name):
            cs = get_center_settings()
            if (cs.fx_source or "cbu") != "cbu":
                results[center.schema_name] = None
                continue
            rate = _mock_cbu_rate() if use_mock else _live_cbu_rate()
            if rate is not None:
                cache.set(f"finance:fx_rate_usd:{center.schema_name}", str(rate), timeout=24 * 3600)
            results[center.schema_name] = str(rate) if rate is not None else None
    return results


def _mock_cbu_rate():
    from decimal import Decimal

    return Decimal("12500.0000")  # deterministic dev/test UZS per USD


def _live_cbu_rate():
    """Fetch the live CBU UZS->USD rate. `requests` is available; import lazily."""
    from decimal import Decimal

    import requests

    resp = requests.get("https://cbu.uz/uz/arkhiv-kursov-valyut/json/USD/", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and data:
        return Decimal(str(data[0]["Rate"]))
    return None
