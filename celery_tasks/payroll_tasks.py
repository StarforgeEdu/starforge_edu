"""Tenant-bound, retry-safe payroll export rendering."""

from __future__ import annotations

from config.celery import app


@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def build_payroll_export(self, export_id: int) -> str | None:
    from apps.payroll.services import (
        build_export,
        mark_export_failed,
        reset_export_for_retry,
    )
    from core.exceptions import ConflictException

    try:
        return build_export(export_id)
    except Exception as exc:
        if isinstance(exc, ConflictException) and exc.code == "payroll_export_in_progress":
            raise self.retry(exc=exc, countdown=5) from exc
        safe_exc = RuntimeError("Payroll export failed.")
        if self.request.retries >= self.max_retries:
            mark_export_failed(export_id, exc)
            raise safe_exc from None
        reset_export_for_retry(export_id)
        raise self.retry(exc=safe_exc) from None
