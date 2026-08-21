"""Printing lane tests (D4-LD).

Covers the full "Tests required" contract: concurrent claim atomicity (threaded,
transaction=True), agent auth (valid/revoked/unknown/cross-branch), the status
transition matrix incl. illegal->409, retry exhaustion (1 notification + audit),
the quota edge, cross-tenant isolation, per-role perms, and query budgets.
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, connections, transaction
from django.test import override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.printing import services
from apps.printing.models import BranchAgent, PrintJob
from apps.printing.tests.factories import BranchAgentFactory, PrinterFactory, PrintJobFactory
from core.permissions import Role
from core.utils import stable_hash

pytestmark = pytest.mark.django_db

JOBS_URL = "/api/v1/printing/jobs/"
PRINTERS_URL = "/api/v1/printing/printers/"
AGENTS_URL = "/api/v1/printing/agents/"
CLAIM_URL = "/api/v1/printing/agent/claim/"


def _status_url(job_id: int) -> str:
    return f"/api/v1/printing/agent/jobs/{job_id}/status/"


def _heartbeat_url(job_id: int) -> str:
    return f"/api/v1/printing/agent/jobs/{job_id}/heartbeat/"


def _job_url(job_id: int) -> str:
    return f"{JOBS_URL}{job_id}/"


def _printer_url(printer_id: int) -> str:
    return f"{PRINTERS_URL}{printer_id}/"


def _agent_url(agent_id: int) -> str:
    return f"{AGENTS_URL}{agent_id}/"


def _agent_client(client_for, tenant, raw_token: str):
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Agent {raw_token}")
    return client


def _status_payload(job: PrintJob, status: str, **extra):
    assert job.lease_id is not None
    return {"lease_id": str(job.lease_id), "status": status, **extra}


def _force_test_claim(job: PrintJob, agent: BranchAgent) -> PrintJob:
    """Install one internally consistent active lease for service-level tests."""

    now = timezone.now()
    PrintJob.objects.filter(pk=job.pk).update(
        status=PrintJob.Status.PICKED,
        agent=agent,
        claimed_at=now,
        lease_id=uuid.uuid4(),
        last_heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=10),
        reconciliation_required_at=None,
        reconciliation_reason="",
        reconciliation_previous_status="",
        next_attempt_at=None,
    )
    job.refresh_from_db()
    return job


# --------------------------------------------------------------------------- #
# register_agent — hashed token, raw never stored (D4-LD-2)
# --------------------------------------------------------------------------- #
def test_register_agent_stores_only_hash(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw_token = services.register_agent(branch_id=branch.pk, name="Desk")
        assert agent.token_hash == stable_hash(raw_token)
        # The raw token appears NOWHERE in the persisted row.
        agent.refresh_from_db()
        for value in (agent.token_hash, agent.name):
            assert raw_token not in (value or "")
        assert agent.token_hash != raw_token


# --------------------------------------------------------------------------- #
# Agent auth: valid / revoked / unknown / cross-branch (D4-LD-2/3)
# --------------------------------------------------------------------------- #
def test_agent_claim_valid_token_returns_job(tenant_a, client_for, monkeypatch):
    from apps.printing.views.v1 import printing_views as views
    from apps.reports.models import ReportRun
    from apps.reports.tests.factories import ReportRunFactory

    signed_requests: list[tuple[str, int]] = []

    def _presign(key, **kwargs):
        signed_requests.append((key, kwargs["expires_in"]))
        return f"signed://{key}"

    monkeypatch.setattr(views, "presign_download", _presign)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        run = ReportRunFactory(
            status=ReportRun.Status.DONE,
            params={"_scope_branch_ids": [branch.pk]},
        )
        key = f"{tenant_a.schema_name}/reports/{run.pk}.pdf"
        run.s3_key = key
        run.save(update_fields=["s3_key"])
        job = PrintJobFactory(
            branch=branch,
            source=PrintJob.Source.REPORT,
            source_id=run.pk,
            payload_s3_key=key,
            cohort_id=None,
            next_attempt_at=timezone.now(),
        )

    resp = _agent_client(client_for, tenant_a, raw).post(CLAIM_URL)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["job"]["id"] == job.pk
    assert body["job"]["status"] == PrintJob.Status.PICKED
    assert uuid.UUID(body["job"]["lease_id"])
    assert body["job"]["lease_expires_at"] is not None
    assert body["download_url"].startswith("signed://")
    assert len(signed_requests) == 1
    assert signed_requests[0][0] == key
    assert 1 <= signed_requests[0][1] <= 600
    assert "payload_s3_key" not in body["job"]
    assert "last_error" not in body["job"]
    assert {
        "branch",
        "agent",
        "source_id",
        "cohort_id",
        "requested_by",
        "created_at",
        "claimed_at",
        "finished_at",
    }.isdisjoint(body["job"])


def test_agent_claim_rolls_back_when_download_capability_cannot_be_issued(
    tenant_a,
    client_for,
    monkeypatch,
):
    from django.core.exceptions import ImproperlyConfigured

    from apps.printing.views.v1 import printing_views as views
    from apps.reports.models import ReportRun
    from apps.reports.tests.factories import ReportRunFactory

    def _unavailable(_key, **_kwargs):
        raise ImproperlyConfigured("private storage configuration detail")

    monkeypatch.setattr(views, "presign_download", _unavailable)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        run = ReportRunFactory(
            status=ReportRun.Status.DONE,
            params={"_scope_branch_ids": [branch.pk]},
        )
        key = f"{tenant_a.schema_name}/reports/{run.pk}.pdf"
        run.s3_key = key
        run.save(update_fields=["s3_key"])
        job = PrintJobFactory(
            branch=branch,
            source=PrintJob.Source.REPORT,
            source_id=run.pk,
            payload_s3_key=key,
            next_attempt_at=timezone.now(),
        )

    response = _agent_client(client_for, tenant_a, raw).post(CLAIM_URL)

    assert response.status_code == 503
    assert response.json()["code"] == "print_download_unavailable"
    assert "private storage" not in repr(response.json())
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        agent.refresh_from_db()
        assert job.status == PrintJob.Status.QUEUED
        assert job.agent_id is None
        assert job.printer_id is None
        assert job.claimed_at is None
        assert job.lease_id is None
        assert job.lease_expires_at is None
        assert agent.last_seen_at is None


def test_agent_claim_empty_queue_returns_204(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _, raw = services.register_agent(branch_id=branch.pk, name="A")
    resp = _agent_client(client_for, tenant_a, raw).post(CLAIM_URL)
    assert resp.status_code == 204


@override_settings(
    API_RATELIMIT_PREAUTH="100/min",
    API_RATELIMIT_ANON="1/min",
    API_RATELIMIT_AGENT="2/min",
)
def test_agent_requests_use_stable_device_bucket_not_anonymous_ip(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _first, first_token = services.register_agent(branch_id=branch.pk, name="A1")
        _second, second_token = services.register_agent(branch_id=branch.pk, name="A2")

    first_client = _agent_client(client_for, tenant_a, first_token)
    assert first_client.post(CLAIM_URL).status_code == 204
    # This exceeds the 1/min anonymous allowance and proves an exact Agent header
    # is not accidentally charged to that shared IP bucket.
    assert first_client.post(CLAIM_URL).status_code == 204
    assert first_client.post(CLAIM_URL).status_code == 429

    # A second authenticated device behind the same source IP has its own stable
    # post-authentication allowance (while both still paid the pre-auth IP cap).
    second_client = _agent_client(client_for, tenant_a, second_token)
    assert second_client.post(CLAIM_URL).status_code == 204


def test_agent_claim_rejects_nonempty_body_without_mutating_queue(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, next_attempt_at=timezone.now())

    response = _agent_client(client_for, tenant_a, raw).post(
        CLAIM_URL,
        {"job_id": job.pk},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    assert "job_id" in response.json()["errors"]
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.status == PrintJob.Status.QUEUED
        assert job.agent_id is None


def test_agent_revoked_token_rejected_401(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        services.revoke_agent(agent_id=agent.pk)
    resp = _agent_client(client_for, tenant_a, raw).post(CLAIM_URL)
    assert resp.status_code == 401
    assert resp.json()["code"] == "agent_token_invalid"


def test_status_service_rechecks_revocation_after_authentication(tenant_a):
    """A stale in-memory agent from before revocation is no mutation authority."""

    from core.exceptions import AuthenticationException

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, _raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)
        services.revoke_agent(agent_id=agent.pk)

        with pytest.raises(AuthenticationException) as caught:
            services.update_job_status(
                agent=agent,
                job_id=job.pk,
                lease_id=job.lease_id,
                status="printing",
            )

        assert caught.value.code == "agent_token_invalid"
        job.refresh_from_db()
        assert job.status == PrintJob.Status.PICKED


def test_invalid_claim_rejection_rechecks_revocation_after_authentication(tenant_a):
    """The invalid-source quarantine is still a mutation owned by the live device."""

    from core.exceptions import AuthenticationException

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, _raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)
        services.revoke_agent(agent_id=agent.pk)

        with pytest.raises(AuthenticationException) as caught:
            services.reject_invalid_claim(agent=agent, job_id=job.pk)

        assert caught.value.code == "agent_token_invalid"
        job.refresh_from_db()
        assert job.status == PrintJob.Status.PICKED


def test_agent_unknown_token_rejected_401(tenant_a, client_for):
    resp = _agent_client(client_for, tenant_a, "deadbeef-not-a-real-token").post(CLAIM_URL)
    assert resp.status_code == 401
    assert resp.json()["code"] == "agent_token_invalid"


def test_agent_missing_token_rejected_401(tenant_a, client_for):
    resp = client_for(tenant_a).post(CLAIM_URL)
    assert resp.status_code == 401


def test_agent_cannot_claim_other_branch_job(tenant_a, client_for, monkeypatch):
    from apps.printing.views.v1 import printing_views as views

    monkeypatch.setattr(views, "presign_download", lambda key, **kw: "signed://x")
    with schema_context(tenant_a.schema_name):
        branch_x = BranchFactory(slug="branch-x")
        branch_y = BranchFactory(slug="branch-y")
        _, raw_x = services.register_agent(branch_id=branch_x.pk, name="X")
        PrintJobFactory(branch=branch_y, next_attempt_at=timezone.now())
    # Agent X's queue is empty (the only job is branch Y's) -> 204, never branch Y's.
    resp = _agent_client(client_for, tenant_a, raw_x).post(CLAIM_URL)
    assert resp.status_code == 204


def test_agent_cannot_update_other_branch_job_404(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch_x = BranchFactory(slug="bx")
        branch_y = BranchFactory(slug="by")
        _agent_x, raw_x = services.register_agent(branch_id=branch_x.pk, name="X")
        job_y = PrintJobFactory(branch=branch_y, status=PrintJob.Status.PICKED)
    resp = _agent_client(client_for, tenant_a, raw_x).post(
        _status_url(job_y.pk), _status_payload(job_y, "printing"), format="json"
    )
    assert resp.status_code == 404


def test_agent_cannot_update_another_agents_job_in_the_same_branch(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        owner, _owner_token = services.register_agent(branch_id=branch.pk, name="Owner")
        intruder, intruder_token = services.register_agent(branch_id=branch.pk, name="Intruder")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=owner)

    response = _agent_client(client_for, tenant_a, intruder_token).post(
        _status_url(job.pk),
        _status_payload(job, "printing"),
        format="json",
    )
    assert response.status_code == 404

    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        intruder.refresh_from_db()
        assert job.status == PrintJob.Status.PICKED
        assert job.agent_id == owner.pk
        assert intruder.last_seen_at is None


# --------------------------------------------------------------------------- #
# Transition matrix incl. illegal -> 409 (D4-LD-3)
# --------------------------------------------------------------------------- #
def test_transition_picked_to_printing_to_done(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)
    client = _agent_client(client_for, tenant_a, raw)
    assert (
        client.post(_status_url(job.pk), _status_payload(job, "printing"), format="json").status_code == 200
    )
    # A lost response can be retried against the same lease without creating a
    # new physical attempt; it also carries monotonic progress once PRINTING was
    # acknowledged.
    retried = client.post(
        _status_url(job.pk),
        _status_payload(job, "printing", pages_printed=1),
        format="json",
    )
    assert retried.status_code == 200
    assert retried.json()["data"]["pages_printed"] == 1
    resp = client.post(
        _status_url(job.pk),
        _status_payload(job, "done", pages_printed=3),
        format="json",
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "done"
    assert {
        "branch",
        "agent",
        "source_id",
        "cohort_id",
        "requested_by",
        "created_at",
        "claimed_at",
        "finished_at",
    }.isdisjoint(resp.json()["data"])
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.pages_printed == 3
        assert job.finished_at is not None


def test_done_without_progress_field_records_the_complete_authorized_total(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.PRINTING,
            agent=agent,
            pages=4,
            copies=2,
        )

    response = _agent_client(client_for, tenant_a, raw).post(
        _status_url(job.pk),
        _status_payload(job, "done"),
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["data"]["pages_printed"] == 8
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.status == PrintJob.Status.DONE
        assert job.pages_printed == 8


def test_agent_status_rejects_unknown_or_semantically_inapplicable_fields(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)
    client = _agent_client(client_for, tenant_a, raw)

    unknown = client.post(
        _status_url(job.pk),
        {
            **_status_payload(job, "printing"),
            "payload_s3_key": "another/tenant/private.pdf",
        },
        format="json",
    )
    assert unknown.status_code == 400
    assert "payload_s3_key" in unknown.json()["errors"]

    misplaced_error = client.post(
        _status_url(job.pk),
        _status_payload(job, "printing", error="ignored device failure"),
        format="json",
    )
    assert misplaced_error.status_code == 400
    assert "error" in misplaced_error.json()["errors"]

    null_progress = client.post(
        _status_url(job.pk),
        _status_payload(job, "printing", pages_printed=None),
        format="json",
    )
    assert null_progress.status_code == 400
    assert "pages_printed" in null_progress.json()["errors"]

    string_progress = client.post(
        _status_url(job.pk),
        _status_payload(job, "printing", pages_printed="1"),
        format="json",
    )
    assert string_progress.status_code == 400
    assert "pages_printed" in string_progress.json()["errors"]


def test_agent_page_progress_is_monotonic_and_bounded_by_authorized_total(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.PICKED,
            agent=agent,
            pages=4,
            copies=2,
        )
    client = _agent_client(client_for, tenant_a, raw)

    started = client.post(_status_url(job.pk), _status_payload(job, "printing"), format="json")
    assert started.status_code == 200
    progressed = client.post(
        _heartbeat_url(job.pk),
        {"lease_id": str(job.lease_id), "pages_printed": 5},
        format="json",
    )
    assert progressed.status_code == 200

    decreasing = client.post(
        _status_url(job.pk),
        _status_payload(job, "done", pages_printed=4),
        format="json",
    )
    assert decreasing.status_code == 400
    assert "pages_printed" in decreasing.json()["errors"]

    excessive = client.post(
        _status_url(job.pk),
        _status_payload(job, "done", pages_printed=9),
        format="json",
    )
    assert excessive.status_code == 400
    assert "pages_printed" in excessive.json()["errors"]

    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.status == PrintJob.Status.PRINTING
        assert job.pages_printed == 5


@pytest.mark.parametrize(
    ("start", "to"),
    [
        (PrintJob.Status.PICKED, "done"),  # skip printing
    ],
)
def test_illegal_transitions_409(tenant_a, client_for, start, to):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=start, agent=agent)
    resp = _agent_client(client_for, tenant_a, raw).post(
        _status_url(job.pk),
        _status_payload(job, to),
        format="json",
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "invalid_transition"


# --------------------------------------------------------------------------- #
# Retry policy + exhaustion (D4-LD-4): 3 fails -> final failed + 1 notif + audit
# --------------------------------------------------------------------------- #
def test_retry_backoff_requeues_until_exhausted(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, _ = services.register_agent(branch_id=branch.pk, name="A")

        def _fail_once(j):
            # A zero-progress failure before printing began is safe to retry.
            return services.update_job_status(
                agent=agent,
                job_id=j.pk,
                lease_id=j.lease_id,
                status="failed",
                error="boom",
            )

        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)

        # 1st failure -> requeued, attempts=1, backoff 2^1*60s.
        job = _fail_once(job)
        assert job.status == PrintJob.Status.QUEUED
        assert job.attempts == 1
        assert job.next_attempt_at is not None

        # 2nd failure -> requeued, attempts=2.
        job = _force_test_claim(job, agent)
        job = _fail_once(job)
        assert job.status == PrintJob.Status.QUEUED
        assert job.attempts == 2

        # 3rd failure -> final failed.
        job = _force_test_claim(job, agent)
        job = _fail_once(job)
        assert job.status == PrintJob.Status.FAILED
        assert job.attempts == 3
        assert job.next_attempt_at is None


def test_failure_after_physical_progress_requires_reconciliation(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, _raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.PICKED,
            agent=agent,
            pages=4,
            copies=1,
        )
        services.update_job_status(
            agent=agent,
            job_id=job.pk,
            lease_id=job.lease_id,
            status=PrintJob.Status.PRINTING,
        )
        services.heartbeat_job(
            agent=agent,
            job_id=job.pk,
            lease_id=job.lease_id,
            pages_printed=3,
        )

        quarantined = services.update_job_status(
            agent=agent,
            job_id=job.pk,
            lease_id=job.lease_id,
            status=PrintJob.Status.FAILED,
            error="paper jam",
            pages_printed=3,
        )

        assert quarantined.status == PrintJob.Status.RECONCILIATION_REQUIRED
        assert quarantined.pages_printed == 3
        assert quarantined.agent_id == agent.pk
        assert quarantined.lease_id is not None
        assert quarantined.next_attempt_at is None


def test_retry_exhaustion_emits_one_notification_and_audit(
    tenant_a, user_in, django_capture_on_commit_callbacks
):
    # CELERY_TASK_ALWAYS_EAGER is on in config.settings.test, so the dispatched
    # notification task runs inline once the on_commit hook fires.
    from apps.audit.models import AuditLog
    from apps.notifications.models import EventType, Notification

    requester = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, _ = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(
            branch=branch, status=PrintJob.Status.PICKED, agent=agent, requested_by=requester
        )

        # 3 failures: the first two requeue, the third is final and emits the
        # notification (via on_commit) + the print.job_failed audit row.
        with django_capture_on_commit_callbacks(execute=True):
            for _ in range(3):
                job = _force_test_claim(job, agent)
                services.update_job_status(
                    agent=agent,
                    job_id=job.pk,
                    lease_id=job.lease_id,
                    status="failed",
                    error="x",
                )

        job.refresh_from_db()
        assert job.status == PrintJob.Status.FAILED
        assert job.attempts == 3

        notifs = Notification.objects.filter(user=requester, event_type=EventType.PRINT_JOB_FAILED)
        assert notifs.count() == 1  # exactly one final-failure notification

        failed_audits = AuditLog.objects.filter(
            action="print.job_failed", resource_type="printing.PrintJob", resource_id=str(job.pk)
        )
        assert failed_audits.count() == 1


# --------------------------------------------------------------------------- #
# Quota edge (D4-LD-5): exactly-at-limit allowed, one page over -> exceeded
# --------------------------------------------------------------------------- #
def _set_quota(value):
    """Set the (orchestrator-owned) quota knob on the cached CenterSettings obj.

    The field is added centrally (integration_needed). Setting it on the live
    instance exercises the service quota logic without depending on the migration
    having landed in this lane's tree.
    """
    from apps.org.selectors import get_center_settings

    cs = get_center_settings()
    cs.print_quota_pages_per_cohort_term = value
    return cs


def _seed_current_term():
    from datetime import date

    from apps.schedule.models import Term

    return Term.objects.create(
        name="T1",
        academic_year="2026-2027",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        is_current=True,
    )


def test_quota_exactly_at_limit_allowed(tenant_a, monkeypatch):
    from apps.cohorts.tests.factories import CohortFactory

    with schema_context(tenant_a.schema_name):
        _seed_current_term()
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        cs = _set_quota(10)
        monkeypatch.setattr("apps.org.selectors.get_center_settings", lambda: cs)
        # 2 pages x 5 copies = 10 == quota -> allowed.
        job = services.enqueue_print(
            source="report",
            source_id=1,
            payload_s3_key="k1",
            branch_id=branch.pk,
            requested_by=None,
            pages=2,
            copies=5,
            cohort_id=cohort.pk,
        )
        assert job.status == PrintJob.Status.QUEUED


def test_quota_one_over_rejected(tenant_a, monkeypatch):
    from apps.cohorts.tests.factories import CohortFactory
    from core.exceptions import StarforgeError

    with schema_context(tenant_a.schema_name):
        _seed_current_term()
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        cs = _set_quota(10)
        monkeypatch.setattr("apps.org.selectors.get_center_settings", lambda: cs)
        # 11 pages > 10 quota -> print_quota_exceeded.
        with pytest.raises(StarforgeError) as exc:
            services.enqueue_print(
                source="report",
                source_id=2,
                payload_s3_key="k2",
                branch_id=branch.pk,
                requested_by=None,
                pages=11,
                copies=1,
                cohort_id=cohort.pk,
            )
        assert exc.value.code == "print_quota_exceeded"


def test_quota_zero_never_blocks(tenant_a, monkeypatch):
    from apps.cohorts.tests.factories import CohortFactory

    with schema_context(tenant_a.schema_name):
        _seed_current_term()
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        cs = _set_quota(0)  # 0 = unlimited
        monkeypatch.setattr("apps.org.selectors.get_center_settings", lambda: cs)
        job = services.enqueue_print(
            source="report",
            source_id=3,
            payload_s3_key="k3",
            branch_id=branch.pk,
            requested_by=None,
            pages=9999,
            copies=9,
            cohort_id=cohort.pk,
        )
        assert job.status == PrintJob.Status.QUEUED


def test_quota_rejects_cohort_from_another_branch(tenant_a, monkeypatch):
    from apps.cohorts.tests.factories import CohortFactory
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        _seed_current_term()
        job_branch = BranchFactory(slug="quota-job-branch")
        foreign_cohort = CohortFactory(branch=BranchFactory(slug="quota-foreign-branch"))
        settings = _set_quota(10)
        monkeypatch.setattr("apps.org.selectors.get_center_settings", lambda: settings)

        with pytest.raises(ValidationException) as caught:
            services.enqueue_print(
                source="report",
                source_id=4,
                payload_s3_key="k4",
                branch_id=job_branch.pk,
                requested_by=None,
                pages=1,
                cohort_id=foreign_cohort.pk,
            )

        assert caught.value.code == "invalid_cohort_scope"
        assert not PrintJob.objects.filter(source_id=4, payload_s3_key="k4").exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_distinct_jobs_cannot_overrun_cohort_quota(tenant_a, monkeypatch):
    from apps.cohorts.tests.factories import CohortFactory
    from core.exceptions import StarforgeError

    with schema_context(tenant_a.schema_name):
        _seed_current_term()
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        settings = _set_quota(10)
        branch_id, cohort_id = branch.pk, cohort.pk
    monkeypatch.setattr("apps.org.selectors.get_center_settings", lambda: settings)
    monkeypatch.setattr("celery_tasks.print_tasks.enqueue_print_job.delay", lambda *_a, **_kw: None)

    barrier = threading.Barrier(2)
    result_lock = threading.Lock()
    results: list[tuple[str, object]] = []

    def _enqueue(source_id: int) -> None:
        barrier.wait()
        try:
            with schema_context(tenant_a.schema_name):
                job = services.enqueue_print(
                    source="report",
                    source_id=source_id,
                    payload_s3_key=f"quota/{source_id}.pdf",
                    branch_id=branch_id,
                    requested_by=None,
                    pages=6,
                    cohort_id=cohort_id,
                )
                result = ("ok", job.pk)
        except StarforgeError as exc:
            result = ("error", exc.code)
        finally:
            connections.close_all()
        with result_lock:
            results.append(result)

    first = threading.Thread(target=_enqueue, args=(501,))
    second = threading.Thread(target=_enqueue, args=(502,))
    first.start()
    second.start()
    first.join()
    second.join()

    assert sorted(kind for kind, _value in results) == ["error", "ok"]
    assert {value for kind, value in results if kind == "error"} == {"print_quota_exceeded"}
    with schema_context(tenant_a.schema_name):
        jobs = PrintJob.objects.filter(cohort_id=cohort_id, source_id__in=(501, 502))
        assert jobs.count() == 1
        assert sum(job.pages * job.copies for job in jobs) == 6
        jobs.delete()
        connection.close()


@pytest.mark.django_db(transaction=True)
def test_concurrent_exact_retry_at_quota_returns_one_open_job(tenant_a, monkeypatch):
    from apps.cohorts.tests.factories import CohortFactory

    with schema_context(tenant_a.schema_name):
        _seed_current_term()
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        settings = _set_quota(6)
        branch_id, cohort_id = branch.pk, cohort.pk
    monkeypatch.setattr("apps.org.selectors.get_center_settings", lambda: settings)
    monkeypatch.setattr("celery_tasks.print_tasks.enqueue_print_job.delay", lambda *_a, **_kw: None)

    barrier = threading.Barrier(2)
    result_lock = threading.Lock()
    job_ids: list[int] = []

    def _enqueue() -> None:
        barrier.wait()
        try:
            with schema_context(tenant_a.schema_name):
                job = services.enqueue_print(
                    source="report",
                    source_id=503,
                    payload_s3_key="quota/503.pdf",
                    branch_id=branch_id,
                    requested_by=None,
                    pages=6,
                    cohort_id=cohort_id,
                )
                with result_lock:
                    job_ids.append(job.pk)
        finally:
            connections.close_all()

    first = threading.Thread(target=_enqueue)
    second = threading.Thread(target=_enqueue)
    first.start()
    second.start()
    first.join()
    second.join()

    assert len(job_ids) == 2
    assert len(set(job_ids)) == 1
    with schema_context(tenant_a.schema_name):
        assert PrintJob.objects.filter(pk=job_ids[0]).count() == 1
        PrintJob.objects.filter(pk=job_ids[0]).delete()
        connection.close()


# --------------------------------------------------------------------------- #
# enqueue_print idempotency (D4-LD-6)
# --------------------------------------------------------------------------- #
def test_enqueue_print_idempotent_on_open_job(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        first = services.enqueue_print(
            source="transcript",
            source_id=7,
            payload_s3_key="t/7.pdf",
            branch_id=branch.pk,
            requested_by=None,
            pages=2,
        )
        second = services.enqueue_print(
            source="transcript",
            source_id=7,
            payload_s3_key="t/7.pdf",
            branch_id=branch.pk,
            requested_by=None,
            pages=2,
        )
        assert first.pk == second.pk
        assert PrintJob.objects.filter(source="transcript", source_id=7).count() == 1


def test_enqueue_print_idempotency_is_branch_scoped(tenant_a):
    """Two branches submitting the SAME (source, source_id, payload) get two
    DISTINCT jobs — the idempotency dedupe must include branch_id, else branch B's
    job is silently routed to branch A's agent."""
    with schema_context(tenant_a.schema_name):
        b1 = BranchFactory()
        b2 = BranchFactory()
        common = dict(source="transcript", source_id=7, payload_s3_key="t/7.pdf", requested_by=None, pages=2)
        j1 = services.enqueue_print(branch_id=b1.pk, **common)
        j2 = services.enqueue_print(branch_id=b2.pk, **common)
        assert j1.pk != j2.pk
        assert j1.branch_id == b1.pk
        assert j2.branch_id == b2.pk
        assert PrintJob.objects.filter(source="transcript", source_id=7).count() == 2


def test_agent_auth_whitespace_only_header_is_not_500(tenant_a, client_for):
    """A whitespace-only Authorization header must not 500 (IndexError) — the
    authenticator defers (no parts) and the request is rejected, not crashed."""
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION="   ")
    resp = client.post(CLAIM_URL, {}, format="json")
    assert resp.status_code in (401, 403)  # rejected, NOT 500


def test_enqueue_print_new_job_after_previous_done(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        first = services.enqueue_print(
            source="receipt",
            source_id=8,
            payload_s3_key="r/8.pdf",
            branch_id=branch.pk,
            requested_by=None,
            pages=1,
        )
        PrintJob.objects.filter(pk=first.pk).update(status=PrintJob.Status.DONE)
        second = services.enqueue_print(
            source="receipt",
            source_id=8,
            payload_s3_key="r/8.pdf",
            branch_id=branch.pk,
            requested_by=None,
            pages=1,
        )
        assert first.pk != second.pk  # a new job once the prior one closed


def test_database_rejects_duplicate_open_print_job(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        common = {
            "branch": branch,
            "source": PrintJob.Source.TRANSCRIPT,
            "source_id": 9,
            "payload_s3_key": "t/9.pdf",
        }
        PrintJobFactory(**common, status=PrintJob.Status.PICKED)

        with pytest.raises(IntegrityError), transaction.atomic():
            PrintJobFactory(**common, status=PrintJob.Status.QUEUED)


def test_database_allows_new_print_job_after_closed_job(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        common = {
            "branch": branch,
            "source": PrintJob.Source.RECEIPT,
            "source_id": 10,
            "payload_s3_key": "r/10.pdf",
        }
        PrintJobFactory(**common, status=PrintJob.Status.DONE)
        PrintJobFactory(**common, status=PrintJob.Status.QUEUED)


# --------------------------------------------------------------------------- #
# Concurrent claim atomicity (D4-LD-3): threaded, transaction=True
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_never_return_same_job(tenant_a):
    from apps.printing.tests.factories import PrinterFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent1, _ = services.register_agent(branch_id=branch.pk, name="A1")
        agent2, _ = services.register_agent(branch_id=branch.pk, name="A2")
        agent1_id, agent2_id = agent1.pk, agent2.pk
        printer1 = PrinterFactory(branch=branch, name="P1")
        printer2 = PrinterFactory(branch=branch, name="P2")
        # Two queued jobs, two agents claiming concurrently.
        PrintJobFactory(branch=branch, source_id=101, next_attempt_at=timezone.now())
        PrintJobFactory(branch=branch, source_id=102, next_attempt_at=timezone.now())

    results: list[int | None] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def _claim(agent_id: int) -> None:
        barrier.wait()
        try:
            with schema_context(tenant_a.schema_name):
                agent = BranchAgent.objects.get(pk=agent_id)
                job = services.claim_job(agent=agent)
                with lock:
                    results.append(job.pk if job else None)
        finally:
            connections.close_all()

    t1 = threading.Thread(target=_claim, args=(agent1_id,))
    t2 = threading.Thread(target=_claim, args=(agent2_id,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    claimed = [r for r in results if r is not None]
    # Each thread claimed a distinct job — never the same one twice.
    assert len(claimed) == len(set(claimed))

    with schema_context(tenant_a.schema_name):
        picked = PrintJob.objects.filter(status=PrintJob.Status.PICKED).count()
        assert picked == len(claimed)
        # Eligible printer rows are locked while load is measured. Concurrent
        # claims therefore balance instead of both observing an empty load map.
        assert set(PrintJob.objects.filter(pk__in=claimed).values_list("printer_id", flat=True)) == {
            printer1.pk,
            printer2.pk,
        }
        # Cleanup for transaction=True (no rollback).
        PrintJob.objects.all().delete()
        from apps.printing.models import Printer

        Printer.objects.all().delete()
        BranchAgent.objects.all().delete()
        connection.close()


# --------------------------------------------------------------------------- #
# Staff endpoints — perms + create (D4-LD-7)
# --------------------------------------------------------------------------- #
def test_staff_create_job_director(as_role, tenant_a):
    client, _user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        from apps.reports.models import ReportRun
        from apps.reports.tests.factories import ReportRunFactory

        branch = BranchFactory()
        run = ReportRunFactory(
            status=ReportRun.Status.DONE,
            params={"_scope_branch_ids": [branch.pk]},
        )
        run.s3_key = f"{tenant_a.schema_name}/reports/{run.pk}.pdf"
        run.save(update_fields=["s3_key"])
    resp = client.post(
        JOBS_URL,
        {
            "source": "report",
            "source_id": run.pk,
            "pages": 2,
            "copies": 1,
        },
        format="json",
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["status"] == "queued"


def test_create_job_rejects_client_storage_and_scope_fields(as_role, tenant_a):
    """Storage capability and routing fields are server-owned, never client DTO."""
    client, _user = as_role(Role.DIRECTOR)
    resp = client.post(
        JOBS_URL,
        {
            "source": "report",
            "source_id": 5,
            "payload_s3_key": "tenant_b/finance/payroll.pdf",  # a foreign-tenant key
            "branch": 999,
            "cohort": 999,
            "pages": 2,
        },
        format="json",
    )
    assert resp.status_code == 400, resp.content
    assert resp.json()["code"] == "validation_error"
    assert set(resp.json()["errors"]) >= {"payload_s3_key", "branch", "cohort"}


def test_staff_create_job_teacher_derives_assignment_key_and_scope(tenant_a, user_in, as_user):
    """A teacher identifies an in-scope assignment; key/branch/cohort come from it."""
    with schema_context(tenant_a.schema_name):
        from apps.assignments.tests.factories import AssignmentFactory
        from apps.cohorts.tests.factories import CohortFactory
        from apps.printing.tests.factories import attach_trusted_assignment_files
        from apps.teachers.tests.factories import TeacherProfileFactory

        branch = BranchFactory()
        teacher = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
        teacher_profile = TeacherProfileFactory(user=teacher, branch=branch)
        cohort = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        assignment = AssignmentFactory(cohort=cohort)
        key = attach_trusted_assignment_files(
            schema=tenant_a.schema_name,
            assignment=assignment,
            filenames=["homework.pdf"],
        )[0]
    client = as_user(tenant_a, teacher)
    resp = client.post(
        JOBS_URL,
        {
            "source": "assignment",
            "source_id": assignment.pk,
            "pages": 1,
        },
        format="json",
    )
    assert resp.status_code == 201
    with schema_context(tenant_a.schema_name):
        job = PrintJob.objects.get(pk=resp.json()["data"]["id"])
        assert job.payload_s3_key == key
        assert job.branch_id == branch.pk
        assert job.cohort_id == cohort.pk


def test_create_job_requires_owning_read_permission_for_the_source(tenant_a, user_in, as_user):
    """R4/PLAUS1: printing:write alone must not let a role pull a sensitive document it
    cannot otherwise read. A registrar holds printing:write but NOT academics:read, so a
    transcript print job (whose key is presign-downloaded at claim time) is forbidden."""
    registrar = user_in(tenant_a, roles=[Role.REGISTRAR])
    resp = as_user(tenant_a, registrar).post(
        JOBS_URL,
        {
            "source": "transcript",
            "source_id": 1,
            "pages": 1,
        },
        format="json",
    )
    assert resp.status_code == 403, resp.content


def test_create_job_cannot_borrow_source_read_permission_from_another_branch(tenant_a, user_in, as_user):
    """A finance grant in A plus printing grant in B must not print B receipts."""
    from apps.payments.models import FiscalReceipt, Payment
    from apps.payments.tests.factories import FiscalReceiptFactory, PaymentFactory
    from apps.users.models import RoleMembership

    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        finance_branch = BranchFactory(slug="finance-grant")
        printing_branch = BranchFactory(slug="printing-grant")
        RoleMembership.objects.create(user=user, branch=finance_branch, role=Role.ACCOUNTANT)
        RoleMembership.objects.create(user=user, branch=printing_branch, role=Role.REGISTRAR)
        payment = PaymentFactory(
            branch_at_payment=finance_branch,
            status=Payment.Status.COMPLETED,
        )
        FiscalReceiptFactory(
            payment=payment,
            status=FiscalReceipt.Status.CONFIRMED,
            pdf_key=f"{tenant_a.schema_name}/receipts/{payment.pk}.pdf",
        )
        user.refresh_from_db()

    response = as_user(tenant_a, user).post(
        JOBS_URL,
        {
            "source": "receipt",
            "source_id": payment.pk,
            "pages": 1,
        },
        format="json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "out_of_scope"


@pytest.mark.parametrize("role", [Role.STUDENT, Role.PARENT])
def test_student_parent_cannot_create_job(as_role, tenant_a, role):
    client, _ = as_role(role)
    resp = client.post(
        JOBS_URL,
        {"source": "report", "source_id": 9, "pages": 1},
        format="json",
    )
    assert resp.status_code == 403


def test_job_list_anonymous_denied(tenant_a, client_for):
    assert client_for(tenant_a).get(JOBS_URL).status_code == 401


@pytest.mark.parametrize("role", [Role.STUDENT, Role.PARENT])
def test_job_list_denied_roles(as_role, role):
    resp = as_role(role)[0].get(JOBS_URL)
    assert resp.status_code == 403


def test_staff_printing_reads_use_exact_active_permission_memberships(tenant_a, user_in, as_user, as_role):
    """A grant in one branch cannot borrow an unrelated/revoked membership's scope.

    All three staff collections are filtered before pagination and direct cross-branch
    object URLs return 404 so they do not disclose whether the resource exists.
    """
    from apps.users.models import RoleMembership

    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        allowed = BranchFactory(slug="print-allowed")
        unrelated = BranchFactory(slug="print-unrelated")
        revoked = BranchFactory(slug="print-revoked")
        RoleMembership.objects.create(user=user, branch=allowed, role=Role.REGISTRAR)
        # Librarian does not grant printing:read/write. Its branch must not be borrowed
        # from the registrar grant above.
        RoleMembership.objects.create(user=user, branch=unrelated, role=Role.LIBRARIAN)
        RoleMembership.objects.create(
            user=user,
            branch=revoked,
            role=Role.REGISTRAR,
            revoked_at=timezone.now(),
        )

        allowed_job = PrintJobFactory(branch=allowed)
        unrelated_job = PrintJobFactory(branch=unrelated)
        revoked_job = PrintJobFactory(branch=revoked)
        allowed_printer = PrinterFactory(branch=allowed)
        unrelated_printer = PrinterFactory(branch=unrelated)
        revoked_printer = PrinterFactory(branch=revoked)
        allowed_agent = BranchAgentFactory(branch=allowed)
        unrelated_agent = BranchAgentFactory(branch=unrelated)
        revoked_agent = BranchAgentFactory(branch=revoked)
        user.refresh_from_db()  # membership writes rotate token_version

    client = as_user(tenant_a, user)
    expected = (
        (JOBS_URL, allowed_job.pk),
        (PRINTERS_URL, allowed_printer.pk),
        (AGENTS_URL, allowed_agent.pk),
    )
    for url, allowed_pk in expected:
        response = client.get(url)
        assert response.status_code == 200, response.content
        assert {row["id"] for row in response.json()["data"]} == {allowed_pk}
        assert client.get(url, {"branch": unrelated.pk}).json()["data"] == []

    for url in (
        _job_url(unrelated_job.pk),
        _job_url(revoked_job.pk),
        _printer_url(unrelated_printer.pk),
        _printer_url(revoked_printer.pk),
        _agent_url(unrelated_agent.pk),
        _agent_url(revoked_agent.pk),
    ):
        assert client.get(url).status_code == 404

    # Write lookups use printing:write scope too, not a later 403 assertion after an
    # unscoped object fetch.
    assert (
        client.patch(_printer_url(unrelated_printer.pk), {"is_active": False}, format="json").status_code
        == 404
    )
    assert client.post(f"{_agent_url(unrelated_agent.pk)}revoke/").status_code == 404

    # Organization leadership remains intentionally unscoped.
    director, _ = as_role(Role.DIRECTOR)
    assert {row["id"] for row in director.get(JOBS_URL).json()["data"]} >= {
        allowed_job.pk,
        unrelated_job.pk,
        revoked_job.pk,
    }
    assert {row["id"] for row in director.get(PRINTERS_URL).json()["data"]} >= {
        allowed_printer.pk,
        unrelated_printer.pk,
        revoked_printer.pk,
    }
    assert {row["id"] for row in director.get(AGENTS_URL).json()["data"]} >= {
        allowed_agent.pk,
        unrelated_agent.pk,
        revoked_agent.pk,
    }
    assert director.get(_job_url(unrelated_job.pk)).status_code == 200
    assert director.get(_printer_url(unrelated_printer.pk)).status_code == 200
    assert director.get(_agent_url(unrelated_agent.pk)).status_code == 200


def test_staff_printing_responses_redact_storage_errors_and_device_secrets(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        job = PrintJobFactory(
            branch=branch,
            status=PrintJob.Status.FAILED,
            payload_s3_key="tenant/private/payroll.pdf",
            last_error="socket://admin:device-password@10.0.0.9 driver stack /private/path",
        )
        printer = PrinterFactory(
            branch=branch,
            capabilities={
                "color": True,
                "duplex": False,
                "paper": ["A4", "Letter", "device-password", 7],
                "password": "device-password",
                "connection_uri": "ipp://admin:device-password@10.0.0.9",
                "credentials": {"token": "device-password"},
            },
        )
        agent = BranchAgentFactory(branch=branch, token_hash=stable_hash("raw-device-token"))

    for response in (client.get(JOBS_URL), client.get(_job_url(job.pk))):
        assert response.status_code == 200
        serialized = repr(response.json())
        assert "payload_s3_key" not in serialized
        assert "last_error" not in serialized
        assert "payroll.pdf" not in serialized
        assert "device-password" not in serialized
        assert "/private/path" not in serialized

    for response in (client.get(PRINTERS_URL), client.get(_printer_url(printer.pk))):
        assert response.status_code == 200
        serialized = repr(response.json())
        assert "device-password" not in serialized
        assert "connection_uri" not in serialized
        assert "credentials" not in serialized
        row = (
            response.json()["data"][0]
            if isinstance(response.json()["data"], list)
            else response.json()["data"]
        )
        assert row["capabilities"] == {"color": True, "duplex": False, "paper": ["A4", "LETTER"]}

    for response in (client.get(AGENTS_URL), client.get(_agent_url(agent.pk))):
        assert response.status_code == 200
        serialized = repr(response.json())
        assert "token_hash" not in serialized
        assert "raw-device-token" not in serialized


def test_register_agent_endpoint_returns_token_once(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = client.post(AGENTS_URL, {"branch": branch.pk, "name": "Desk"}, format="json")
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert "token" in body
    assert len(body["token"]) >= 32
    # The token returned is raw; the DB stores only its hash.
    with schema_context(tenant_a.schema_name):
        agent = BranchAgent.objects.get(pk=body["id"])
        assert agent.token_hash == stable_hash(body["token"])
    # Listing agents never exposes the token.
    list_body = client.get(AGENTS_URL).json()
    assert all("token" not in row and "token_hash" not in row for row in list_body["data"])


@pytest.mark.parametrize("role", [Role.STUDENT, Role.PARENT])
def test_register_agent_denied_without_printing_write(as_role, tenant_a, role):
    # Roles lacking printing:write cannot register an agent (matrix gate).
    client, _ = as_role(role)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = client.post(AGENTS_URL, {"branch": branch.pk, "name": "X"}, format="json")
    assert resp.status_code == 403


def test_register_agent_registrar_own_branch_allowed(tenant_a, user_in, as_user):
    # A manager (registrar, printing:write) registers an agent in their own branch.
    registrar = user_in(tenant_a, roles=[Role.REGISTRAR])
    with schema_context(tenant_a.schema_name):
        branch_id = next(m.branch_id for m in registrar.role_memberships.all() if m.role == Role.REGISTRAR)
    client = as_user(tenant_a, registrar)
    resp = client.post(AGENTS_URL, {"branch": branch_id, "name": "Desk"}, format="json")
    assert resp.status_code == 201


def test_printer_patch_explicit_null_rejected(as_role, tenant_a):
    """PATCH of a NOT NULL column with an explicit JSON null is a 400, not a silent
    coerce-to-default that would wipe the printer's capabilities."""
    from apps.printing.models import Printer

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        printer = Printer.objects.create(branch=branch, name="P1", capabilities={"color": True})
    resp = client.patch(f"{PRINTERS_URL}{printer.pk}/", {"capabilities": None}, format="json")
    assert resp.status_code == 400
    with schema_context(tenant_a.schema_name):
        printer.refresh_from_db()
        assert printer.capabilities == {"color": True}  # not wiped


# --------------------------------------------------------------------------- #
# Cross-tenant isolation (TD-1)
# --------------------------------------------------------------------------- #
def test_jobs_cross_tenant_token_rejected(tenant_a, tenant_b, user_in, client_for):
    from apps.auth.services import issue_token

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        access = issue_token(user)["access"]
    client_b = client_for(tenant_b)
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = client_b.get(JOBS_URL)
    assert resp.status_code == 401
    assert resp.json()["code"] == "authentication_failed"


def test_jobs_not_visible_across_tenants(tenant_a, tenant_b, as_role):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        PrintJobFactory(branch=branch, source_id=7777, payload_s3_key="a/secret.pdf")
    client_b, _ = as_role(Role.DIRECTOR, tenant=tenant_b)
    rows = client_b.get(JOBS_URL).json()["data"]
    assert 7777 not in {row["source_id"] for row in rows}
    # Storage locations are not serialized even inside the owning tenant.
    assert all("payload_s3_key" not in row for row in rows)


def test_agent_token_does_not_authenticate_cross_tenant(tenant_a, tenant_b, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _, raw = services.register_agent(branch_id=branch.pk, name="A")
    # The same raw token presented to tenant_b finds no matching hash there.
    resp = _agent_client(client_for, tenant_b, raw).post(CLAIM_URL)
    assert resp.status_code == 401
    assert resp.json()["code"] == "agent_token_invalid"


# --------------------------------------------------------------------------- #
# Query budget on the list endpoint (DoD #3)
# --------------------------------------------------------------------------- #
def test_jobs_list_query_budget(as_role, tenant_a, django_assert_max_num_queries):
    client, user = as_role(Role.REGISTRAR)
    with schema_context(tenant_a.schema_name):
        branch = next(
            membership.branch
            for membership in user.role_memberships.select_related("branch")
            if membership.role == Role.REGISTRAR
        )
        for i in range(10):
            PrintJobFactory(branch=branch, source_id=200 + i)
    with django_assert_max_num_queries(10):
        resp = client.get(JOBS_URL)
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 10
