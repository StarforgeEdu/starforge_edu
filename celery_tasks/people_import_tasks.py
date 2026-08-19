"""Bounded, retry-safe finalisation of reviewed people imports."""

from __future__ import annotations

from config.celery import app


@app.task(
    bind=True,
    max_retries=2,
    retry_backoff=True,
    retry_jitter=True,
    acks_late=True,
    reject_on_worker_lost=True,
    rate_limit="2/s",
)
def process_people_import(self, draft_id: int) -> None:
    from apps.people_imports.services import mark_processing_failed, process_draft

    try:
        process_draft(draft_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            mark_processing_failed(draft_id)
            raise
        raise self.retry(exc=exc) from exc
