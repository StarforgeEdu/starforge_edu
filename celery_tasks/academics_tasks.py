"""Academics beat/async tasks (D2-C-5). `generate_transcript_pdf` renders a
transcript to PDF (weasyprint) and uploads it to S3 — the body lives in
apps.academics.services. Runs under the per-tenant schema (enqueued with
`_schema_name`), idempotent (a `done` transcript short-circuits), retries ≤3
with exponential backoff. No weasyprint/S3 call ever happens in a request
handler (DoD #9)."""

from __future__ import annotations

from config.celery import app


@app.task(bind=True, max_retries=3, retry_backoff=True)
def generate_transcript_pdf(self, transcript_id: int) -> str | None:
    from apps.academics.services import generate_transcript, mark_transcript_failed

    try:
        return generate_transcript(transcript_id)
    except Exception as exc:  # mark failed, then let Celery retry
        mark_transcript_failed(transcript_id, exc)
        raise self.retry(exc=exc) from exc
