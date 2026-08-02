from __future__ import annotations

import pytest


def test_finance_statement_cache_reuse_accepts_only_exact_task_output():
    from celery_tasks.finance_tasks import _trusted_cached_statement

    schema = "tenant_a"
    key = f"{schema}/documents/statement_17_20260802112233_{'a' * 32}.pdf"
    valid = {
        "key": key,
        "requested_by_id": 8,
        "student_id": 17,
        "invoice_ids": [2, 9],
    }

    assert _trusted_cached_statement(valid, schema=schema, student_id=17, requested_by_id=8) == key
    assert (
        _trusted_cached_statement(
            {**valid, "key": f"another/documents/{key.rsplit('/', 1)[-1]}"},
            schema=schema,
            student_id=17,
            requested_by_id=8,
        )
        is None
    )
    assert (
        _trusted_cached_statement(
            {**valid, "invoice_ids": [9, 2]},
            schema=schema,
            student_id=17,
            requested_by_id=8,
        )
        is None
    )
    assert (
        _trusted_cached_statement(
            {**valid, "requested_by_id": 99},
            schema=schema,
            student_id=17,
            requested_by_id=8,
        )
        is None
    )


def test_finance_statement_retry_does_not_expose_render_or_storage_error(monkeypatch):
    from apps.finance import services
    from celery_tasks.finance_tasks import generate_statement_pdf

    private_error = "signed://private-object?credential=secret"
    monkeypatch.setattr(
        services,
        "generate_statement_artifact",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(private_error)),
    )
    captured: list[Exception] = []

    def retry(*, exc):
        captured.append(exc)
        raise RuntimeError("retry-sentinel")

    monkeypatch.setattr(generate_statement_pdf, "retry", retry)
    with pytest.raises(RuntimeError, match="retry-sentinel"):
        generate_statement_pdf.run(17, requested_by_id=8)

    assert len(captured) == 1
    assert str(captured[0]) == "Finance statement generation failed."
    assert private_error not in str(captured[0])


def test_print_enqueue_retry_does_not_expose_internal_error(monkeypatch):
    from celery_tasks import print_tasks

    private_error = "s3://private-bucket/student-document.pdf"
    monkeypatch.setattr(
        print_tasks,
        "_enqueue_print_job_body",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(private_error)),
    )
    captured: list[Exception] = []

    def retry(*, exc):
        captured.append(exc)
        raise RuntimeError("retry-sentinel")

    monkeypatch.setattr(print_tasks.enqueue_print_job, "retry", retry)
    with pytest.raises(RuntimeError, match="retry-sentinel"):
        print_tasks.enqueue_print_job.run(17)

    assert len(captured) == 1
    assert str(captured[0]) == "Print-job enqueue failed."
    assert private_error not in str(captured[0])
