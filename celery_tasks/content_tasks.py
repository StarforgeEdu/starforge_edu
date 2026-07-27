"""Content storage tasks (D2-E-4). Per-file async validation + thumbnailing;
bodies live in apps.content.services. Enqueued with `_schema_name` so they run
under the right tenant schema. Idempotent (status / existing-thumb short-circuit),
retries <=3 with backoff."""

from __future__ import annotations

from config.celery import app


@app.task(bind=True, max_retries=3, retry_backoff=True)
def validate_uploaded_file(self, file_id: int) -> str:
    from apps.content.services import validate_uploaded_file as _validate

    try:
        return _validate(file_id)
    except Exception as exc:
        raise self.retry(exc=exc) from exc


@app.task(bind=True, max_retries=3, retry_backoff=True)
def generate_thumbnail(self, file_id: int) -> str | None:
    from apps.content.services import generate_thumbnail as _thumb

    try:
        return _thumb(file_id)
    except Exception as exc:
        raise self.retry(exc=exc) from exc
