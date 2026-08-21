from __future__ import annotations

from uuid import uuid4

import pytest


def test_finance_statement_task_delegates_to_one_durable_export_row(monkeypatch):
    from apps.finance import services
    from celery_tasks.finance_tasks import generate_statement_pdf

    export_id = str(uuid4())
    expected_key = f"tenant_a/documents/statements/{export_id}.pdf"
    received_ids: list[str] = []
    monkeypatch.setattr(
        services,
        "build_statement_export",
        lambda received_id: received_ids.append(received_id) or expected_key,
    )

    assert generate_statement_pdf.run(export_id) == expected_key
    assert received_ids == [export_id]


def test_finance_statement_retry_does_not_expose_render_or_storage_error(monkeypatch):
    from apps.finance import services
    from celery_tasks.finance_tasks import generate_statement_pdf

    export_id = str(uuid4())
    private_error = "signed://private-object?credential=secret"

    def raise_private_error(_export_id: str):
        raise RuntimeError(private_error)

    reset_ids: list[str] = []
    monkeypatch.setattr(services, "build_statement_export", raise_private_error)
    monkeypatch.setattr(
        services,
        "reset_statement_export_for_retry",
        lambda received_id: reset_ids.append(received_id),
    )
    captured: list[Exception] = []

    def retry(*, exc):
        captured.append(exc)
        raise RuntimeError("retry-sentinel")

    monkeypatch.setattr(generate_statement_pdf, "retry", retry)
    with pytest.raises(RuntimeError, match="retry-sentinel"):
        generate_statement_pdf.run(export_id)

    assert reset_ids == [export_id]
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
