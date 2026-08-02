"""Physical-print lease, heartbeat, quarantine, and operator reconciliation.

These tests require PostgreSQL row locks and are intentionally run serially.
Nothing in this suite treats an expired device lease as proof that paper was not
produced.
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest
from django.db import DatabaseError, connection, connections, transaction
from django.test import override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.printing import services
from apps.printing.models import PrintJob, PrintJobReconciliation
from apps.printing.tests.factories import PrintJobFactory
from core.exceptions import ConflictException
from core.permissions import Role
from core.utils import stable_hash
from tests.role_principal_helpers import ensure_role_principal, exact_session_client

pytestmark = pytest.mark.django_db


def _heartbeat_url(job_id: int) -> str:
    return f"/api/v1/printing/agent/jobs/{job_id}/heartbeat/"


def _status_url(job_id: int) -> str:
    return f"/api/v1/printing/agent/jobs/{job_id}/status/"


def _reconcile_url(job_id: int) -> str:
    return f"/api/v1/printing/jobs/{job_id}/reconcile/"


def _history_url(job_id: int) -> str:
    return f"/api/v1/printing/jobs/{job_id}/reconciliations/"


def _agent_client(client_for, tenant, raw_token: str):
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Agent {raw_token}")
    return client


def _reconciliation_body(outcome: str = "confirmed_not_printed") -> dict[str, str]:
    return {"outcome": outcome, "evidence_reference": "INC-PRINT-2026-0001"}


@override_settings(PRINT_AGENT_LEASE_SECONDS=600)
def test_heartbeat_renews_only_the_same_live_attempt(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PRINTING, agent=agent)
        old_expiry = timezone.now() + timedelta(minutes=1)
        PrintJob.objects.filter(pk=job.pk).update(lease_expires_at=old_expiry)
        job.refresh_from_db()

    response = _agent_client(client_for, tenant_a, raw).post(
        _heartbeat_url(job.pk),
        {"lease_id": str(job.lease_id), "pages_printed": 1},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["data"]["lease_id"] == str(job.lease_id)
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        # A heartbeat renews this exact attempt; it must never rewind physical
        # state from printing back to picked.
        assert job.status == PrintJob.Status.PRINTING
        assert job.pages_printed == 1
        assert job.lease_expires_at > old_expiry


def test_picked_heartbeat_cannot_claim_physical_page_progress(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)

    response = _agent_client(client_for, tenant_a, raw).post(
        _heartbeat_url(job.pk),
        {"lease_id": str(job.lease_id), "pages_printed": 1},
        format="json",
    )

    assert response.status_code == 400
    assert "pages_printed" in response.json()["errors"]
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.status == PrintJob.Status.PICKED
        assert job.pages_printed == 0


def test_missing_malformed_or_foreign_lease_cannot_report_status(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)
    client = _agent_client(client_for, tenant_a, raw)

    missing = client.post(_status_url(job.pk), {"status": "printing"}, format="json")
    malformed = client.post(
        _status_url(job.pk),
        {"lease_id": str(job.lease_id).upper(), "status": "printing"},
        format="json",
    )
    foreign = client.post(
        _status_url(job.pk),
        {"lease_id": str(uuid.uuid4()), "status": "printing"},
        format="json",
    )

    assert missing.status_code == 400
    assert malformed.status_code == 400
    assert foreign.status_code == 404
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.status == PrintJob.Status.PICKED


def test_expired_heartbeat_quarantines_and_never_requeues(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PRINTING, agent=agent)
        PrintJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        job.refresh_from_db()

    client = _agent_client(client_for, tenant_a, raw)
    response = client.post(
        _heartbeat_url(job.pk),
        {"lease_id": str(job.lease_id)},
        format="json",
    )
    late_status = client.post(
        _status_url(job.pk),
        {"lease_id": str(job.lease_id), "status": "done"},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "print_reconciliation_required"
    assert late_status.status_code == 409
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.status == PrintJob.Status.RECONCILIATION_REQUIRED
        assert job.next_attempt_at is None
        assert job.reconciliation_previous_status == PrintJob.Status.PRINTING


def test_agent_failure_after_printing_began_is_not_automatically_replayed(
    tenant_a,
    client_for,
):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.PRINTING,
            agent=agent,
            pages_printed=1,
        )

    response = _agent_client(client_for, tenant_a, raw).post(
        _status_url(job.pk),
        {
            "lease_id": str(job.lease_id),
            "status": "failed",
            "pages_printed": 1,
            "error": "paper jam",
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "print_reconciliation_required"
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.status == PrintJob.Status.RECONCILIATION_REQUIRED
        assert job.pages_printed == 1
        assert job.next_attempt_at is None
        assert (
            job.reconciliation_reason
            == PrintJob.ReconciliationReason.AGENT_REPORTED_FAILURE
        )


def test_stale_sweep_is_bounded_idempotent_and_never_requeues(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        jobs = [PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED) for _ in range(3)]
        PrintJob.objects.filter(pk__in=[job.pk for job in jobs]).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

        assert services.quarantine_stale_print_leases(batch_size=2) == 2
        assert PrintJob.objects.filter(status=PrintJob.Status.RECONCILIATION_REQUIRED).count() == 2
        assert PrintJob.objects.filter(status=PrintJob.Status.QUEUED).count() == 0
        assert services.quarantine_stale_print_leases(batch_size=2) == 1
        assert services.quarantine_stale_print_leases(batch_size=2) == 0


@pytest.mark.django_db(transaction=True)
def test_heartbeat_and_stale_sweep_serialize_without_duplicate_output(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, _raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)
        cutoff = timezone.now() + timedelta(minutes=5)
        PrintJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() + timedelta(minutes=1))
        job.refresh_from_db()
        job_id, lease_id = job.pk, job.lease_id

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _heartbeat() -> None:
        try:
            barrier.wait()
            with schema_context(tenant_a.schema_name):
                services.heartbeat_job(agent=agent, job_id=job_id, lease_id=lease_id)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connections.close_all()

    def _sweep() -> None:
        try:
            barrier.wait()
            with schema_context(tenant_a.schema_name):
                services.quarantine_stale_print_leases(batch_size=1, now=cutoff)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connections.close_all()

    first = threading.Thread(target=_heartbeat)
    second = threading.Thread(target=_sweep)
    first.start()
    second.start()
    first.join()
    second.join()

    assert errors == []
    with schema_context(tenant_a.schema_name):
        job = PrintJob.objects.get(pk=job_id)
        assert job.status in (
            PrintJob.Status.PICKED,
            PrintJob.Status.RECONCILIATION_REQUIRED,
        )
        if job.status == PrintJob.Status.PICKED:
            assert job.lease_expires_at > cutoff
        else:
            assert job.next_attempt_at is None
        connection.close()


def test_confirmed_not_printed_requeues_once_and_exact_retry_is_idempotent(
    tenant_a,
    user_in,
):
    operator = user_in(tenant_a, roles=[Role.DIRECTOR])
    raw_key = "print-reconcile-key-0001"
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
        )
        first = services.reconcile_print_job(
            job_id=job.pk,
            expected_branch_id=branch.pk,
            actor=operator,
            outcome=PrintJobReconciliation.Outcome.CONFIRMED_NOT_PRINTED,
            evidence_reference="INC-PRINT-2026-0001",
            idempotency_key=raw_key,
        )
        second = services.reconcile_print_job(
            job_id=job.pk,
            expected_branch_id=branch.pk,
            actor=operator,
            outcome=PrintJobReconciliation.Outcome.CONFIRMED_NOT_PRINTED,
            evidence_reference="INC-PRINT-2026-0001",
            idempotency_key=raw_key,
        )

        assert first.pk == second.pk == job.pk
        assert second.status == PrintJob.Status.QUEUED
        assert second.agent_id is None
        assert second.lease_id is None
        assert PrintJobReconciliation.objects.filter(job=job).count() == 1
        record = PrintJobReconciliation.objects.get(job=job)
        assert record.idempotency_key_hash == stable_hash(raw_key)
        assert raw_key not in record.idempotency_key_hash
        assert record.reason == PrintJob.ReconciliationReason.LEASE_EXPIRED


def test_reconciliation_key_reuse_with_different_intent_is_conflict(tenant_a, user_in):
    operator = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
        )
        services.reconcile_print_job(
            job_id=job.pk,
            expected_branch_id=branch.pk,
            actor=operator,
            outcome=PrintJobReconciliation.Outcome.ABANDONED_UNKNOWN,
            evidence_reference="INC-PRINT-2026-0001",
            idempotency_key="print-reconcile-key-0002",
        )
        with pytest.raises(ConflictException) as caught:
            services.reconcile_print_job(
                job_id=job.pk,
                expected_branch_id=branch.pk,
                actor=operator,
                outcome=PrintJobReconciliation.Outcome.CONFIRMED_PRINTED,
                evidence_reference="INC-PRINT-2026-0002",
                idempotency_key="print-reconcile-key-0002",
            )
        assert caught.value.code == "idempotency_key_reused"


def test_reconciliation_evidence_is_database_append_only(tenant_a, user_in):
    operator = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
        )
        services.reconcile_print_job(
            job_id=job.pk,
            expected_branch_id=branch.pk,
            actor=operator,
            outcome=PrintJobReconciliation.Outcome.ABANDONED_UNKNOWN,
            evidence_reference="INC-PRINT-2026-IMMUTABLE",
            idempotency_key="print-reconcile-immutable-0001",
        )
        record = PrintJobReconciliation.objects.get(job=job)

        with pytest.raises(DatabaseError), transaction.atomic():
            PrintJobReconciliation.objects.filter(pk=record.pk).update(evidence_reference="rewritten")
        with pytest.raises(DatabaseError), transaction.atomic():
            PrintJobReconciliation.objects.filter(pk=record.pk).delete()


def test_database_rejects_reconciliation_evidence_for_another_branch(tenant_a, user_in):
    operator = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(slug="print-evidence-correct")
        wrong_branch = BranchFactory(slug="print-evidence-wrong")
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
        )

        with pytest.raises(DatabaseError), transaction.atomic():
            PrintJobReconciliation.objects.create(
                job=job,
                branch=wrong_branch,
                lease_id=job.lease_id,
                previous_status=job.reconciliation_previous_status,
                reason=job.reconciliation_reason,
                outcome=PrintJobReconciliation.Outcome.ABANDONED_UNKNOWN,
                evidence_reference="INC-PRINT-FORGED-BRANCH",
                resolved_by=operator,
                idempotency_key_hash=stable_hash("print-reconcile-forged-branch"),
            )


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        (PrintJobReconciliation.Outcome.CONFIRMED_PRINTED, PrintJob.Status.DONE),
        (PrintJobReconciliation.Outcome.ABANDONED_UNKNOWN, PrintJob.Status.FAILED),
    ],
)
def test_reconciliation_terminal_outcomes_never_requeue(
    tenant_a,
    user_in,
    outcome,
    expected_status,
):
    operator = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
        )
        resolved = services.reconcile_print_job(
            job_id=job.pk,
            expected_branch_id=branch.pk,
            actor=operator,
            outcome=outcome,
            evidence_reference="INC-PRINT-2026-0003",
            idempotency_key=f"print-reconcile-{outcome}-0003",
        )
        assert resolved.status == expected_status
        assert resolved.next_attempt_at is None
        assert resolved.lease_id is None


def test_confirmed_not_printed_still_obeys_max_attempts(tenant_a, user_in):
    operator = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
            attempts=services.MAX_ATTEMPTS - 1,
        )
        resolved = services.reconcile_print_job(
            job_id=job.pk,
            expected_branch_id=branch.pk,
            actor=operator,
            outcome=PrintJobReconciliation.Outcome.CONFIRMED_NOT_PRINTED,
            evidence_reference="INC-PRINT-2026-0004",
            idempotency_key="print-reconcile-key-0004",
        )
        assert resolved.status == PrintJob.Status.FAILED
        assert resolved.attempts == services.MAX_ATTEMPTS
        assert resolved.next_attempt_at is None


def test_old_lease_cannot_report_again_after_reviewed_retry(tenant_a, user_in):
    operator = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, _raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
            agent=agent,
        )
        old_lease = job.lease_id
        services.reconcile_print_job(
            job_id=job.pk,
            expected_branch_id=branch.pk,
            actor=operator,
            outcome=PrintJobReconciliation.Outcome.CONFIRMED_NOT_PRINTED,
            evidence_reference="INC-PRINT-2026-0005",
            idempotency_key="print-reconcile-key-0005",
        )
        claimed = services.claim_job(agent=agent)
        assert claimed is not None
        assert claimed.lease_id != old_lease

        from core.exceptions import NotFoundException

        with pytest.raises(NotFoundException):
            services.update_job_status(
                agent=agent,
                job_id=job.pk,
                lease_id=old_lease,
                status=PrintJob.Status.PRINTING,
            )


def test_reconciliation_endpoints_are_exactly_branch_scoped_and_redacted(
    tenant_a,
    user_in,
    client_for,
):
    with schema_context(tenant_a.schema_name):
        allowed = BranchFactory(slug="print-review-allowed")
        denied = BranchFactory(slug="print-review-denied")
    operator = user_in(tenant_a, roles=[Role.REGISTRAR], branch=allowed)
    with schema_context(tenant_a.schema_name):
        ensure_role_principal(operator, roles=[Role.REGISTRAR], branch=allowed)
        allowed_job = PrintJobFactory(
            branch=allowed,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
        )
        denied_job = PrintJobFactory(
            branch=denied,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
        )
    client = exact_session_client(client_for, tenant_a, operator)
    headers = {"HTTP_IDEMPOTENCY_KEY": "print-reconcile-api-0001"}

    denied_response = client.post(
        _reconcile_url(denied_job.pk),
        _reconciliation_body(),
        format="json",
        **headers,
    )
    allowed_response = client.post(
        _reconcile_url(allowed_job.pk),
        _reconciliation_body(),
        format="json",
        **headers,
    )

    assert denied_response.status_code == 404
    assert allowed_response.status_code == 200
    history = client.get(_history_url(allowed_job.pk))
    assert history.status_code == 200
    serialized = repr(history.json())
    assert "lease_id" not in serialized
    assert "idempotency" not in serialized
    assert client.get(_history_url(denied_job.pk)).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_concurrent_exact_reconciliation_creates_one_evidence_row(tenant_a, user_in):
    operator = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.RECONCILIATION_REQUIRED,
        )
        branch_id, job_id = branch.pk, job.pk

    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []

    def _resolve() -> None:
        try:
            barrier.wait()
            with schema_context(tenant_a.schema_name):
                result = services.reconcile_print_job(
                    job_id=job_id,
                    expected_branch_id=branch_id,
                    actor=operator,
                    outcome=PrintJobReconciliation.Outcome.CONFIRMED_NOT_PRINTED,
                    evidence_reference="INC-PRINT-2026-0006",
                    idempotency_key="print-reconcile-key-0006",
                )
                results.append(result.pk)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connections.close_all()

    first = threading.Thread(target=_resolve)
    second = threading.Thread(target=_resolve)
    first.start()
    second.start()
    first.join()
    second.join()

    assert errors == []
    assert results == [job_id, job_id]
    with schema_context(tenant_a.schema_name):
        assert PrintJobReconciliation.objects.filter(job_id=job_id).count() == 1
        assert PrintJob.objects.get(pk=job_id).status == PrintJob.Status.QUEUED
        connection.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"pages": 3},
        {"copies": 2},
        {"color": True},
        {"duplex": True},
        {"cohort_id": 999_999},
    ],
)
def test_open_print_source_rejects_different_physical_options(tenant_a, changes):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        base = {
            "source": "report",
            "source_id": 8001,
            "payload_s3_key": "reports/8001.pdf",
            "branch_id": branch.pk,
            "requested_by": None,
            "pages": 2,
            "copies": 1,
            "color": False,
            "duplex": False,
            "cohort_id": None,
        }
        first = services.enqueue_print(**base)
        with pytest.raises(ConflictException) as caught:
            services.enqueue_print(**{**base, **changes})
        assert caught.value.code == "print_idempotency_conflict"
        assert PrintJob.objects.filter(pk=first.pk).count() == 1


def test_staff_endpoint_returns_stable_409_for_different_open_print_options(
    tenant_a,
    as_role,
):
    from apps.reports.models import ReportRun
    from apps.reports.tests.factories import ReportRunFactory

    client, _operator = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        run = ReportRunFactory(
            status=ReportRun.Status.DONE,
            params={"_scope_branch_ids": [branch.pk]},
        )
        run.s3_key = f"{tenant_a.schema_name}/reports/{run.pk}.pdf"
        run.save(update_fields=["s3_key"])

    first = client.post(
        "/api/v1/printing/jobs/",
        {"source": "report", "source_id": run.pk, "pages": 1},
        format="json",
    )
    conflict = client.post(
        "/api/v1/printing/jobs/",
        {"source": "report", "source_id": run.pk, "pages": 2},
        format="json",
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "print_idempotency_conflict"


@pytest.mark.django_db(transaction=True)
def test_concurrent_different_print_options_create_one_job_and_one_conflict(
    tenant_a,
    monkeypatch,
):
    monkeypatch.setattr("celery_tasks.print_tasks.enqueue_print_job.delay", lambda *_a, **_kw: None)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        branch_id = branch.pk

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def _enqueue(pages: int) -> None:
        try:
            barrier.wait()
            with schema_context(tenant_a.schema_name):
                services.enqueue_print(
                    source="report",
                    source_id=8002,
                    payload_s3_key="reports/8002.pdf",
                    branch_id=branch_id,
                    requested_by=None,
                    pages=pages,
                )
                outcomes.append("created")
        except ConflictException as exc:
            outcomes.append(exc.code)
        finally:
            connections.close_all()

    first = threading.Thread(target=_enqueue, args=(1,))
    second = threading.Thread(target=_enqueue, args=(2,))
    first.start()
    second.start()
    first.join()
    second.join()

    assert sorted(outcomes) == ["created", "print_idempotency_conflict"]
    with schema_context(tenant_a.schema_name):
        assert PrintJob.objects.filter(source_id=8002).count() == 1
        connection.close()
