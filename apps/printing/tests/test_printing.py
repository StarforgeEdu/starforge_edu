"""Printing lane tests (D4-LD).

Covers the full "Tests required" contract: concurrent claim atomicity (threaded,
transaction=True), agent auth (valid/revoked/unknown/cross-branch), the status
transition matrix incl. illegal->409, retry exhaustion (1 notification + audit),
the quota edge, cross-tenant isolation, per-role perms, and query budgets.
"""

from __future__ import annotations

import threading

import pytest
from django.db import IntegrityError, connection, connections, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.printing import services
from apps.printing.models import BranchAgent, PrintJob
from apps.printing.tests.factories import PrintJobFactory
from core.permissions import Role
from core.utils import stable_hash

pytestmark = pytest.mark.django_db

JOBS_URL = "/api/v1/printing/jobs/"
PRINTERS_URL = "/api/v1/printing/printers/"
AGENTS_URL = "/api/v1/printing/agents/"
CLAIM_URL = "/api/v1/printing/agent/claim/"


def _status_url(job_id: int) -> str:
    return f"/api/v1/printing/agent/jobs/{job_id}/status/"


def _agent_client(client_for, tenant, raw_token: str):
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Agent {raw_token}")
    return client


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

    monkeypatch.setattr(views, "presign_download", lambda key, **kw: f"signed://{key}")
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, next_attempt_at=timezone.now())

    resp = _agent_client(client_for, tenant_a, raw).post(CLAIM_URL)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["job"]["id"] == job.pk
    assert body["download_url"].startswith("signed://")


def test_agent_claim_empty_queue_returns_204(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        _, raw = services.register_agent(branch_id=branch.pk, name="A")
    resp = _agent_client(client_for, tenant_a, raw).post(CLAIM_URL)
    assert resp.status_code == 204


def test_agent_revoked_token_rejected_401(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        services.revoke_agent(agent_id=agent.pk)
    resp = _agent_client(client_for, tenant_a, raw).post(CLAIM_URL)
    assert resp.status_code == 401
    assert resp.json()["code"] == "agent_token_invalid"


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
        _status_url(job_y.pk), {"status": "printing"}, format="json"
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Transition matrix incl. illegal -> 409 (D4-LD-3)
# --------------------------------------------------------------------------- #
def test_transition_picked_to_printing_to_done(tenant_a, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)
    client = _agent_client(client_for, tenant_a, raw)
    assert client.post(_status_url(job.pk), {"status": "printing"}, format="json").status_code == 200
    resp = client.post(_status_url(job.pk), {"status": "done", "pages_printed": 3}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "done"
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.pages_printed == 3
        assert job.finished_at is not None


@pytest.mark.parametrize(
    ("start", "to"),
    [
        (PrintJob.Status.QUEUED, "printing"),  # not picked yet
        (PrintJob.Status.PICKED, "done"),  # skip printing
        (PrintJob.Status.DONE, "printing"),  # terminal
        (PrintJob.Status.FAILED, "done"),  # terminal
        (PrintJob.Status.PRINTING, "printing"),  # no self-loop on printing
    ],
)
def test_illegal_transitions_409(tenant_a, client_for, start, to):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw = services.register_agent(branch_id=branch.pk, name="A")
        job = PrintJobFactory(branch=branch, status=start, agent=agent)
    resp = _agent_client(client_for, tenant_a, raw).post(_status_url(job.pk), {"status": to}, format="json")
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
            # Move to picked->printing then fail (legal path to FAILED).
            services.update_job_status(agent=agent, job_id=j.pk, status="printing")
            return services.update_job_status(agent=agent, job_id=j.pk, status="failed", error="boom")

        job = PrintJobFactory(branch=branch, status=PrintJob.Status.PICKED, agent=agent)

        # 1st failure -> requeued, attempts=1, backoff 2^1*60s.
        job = _fail_once(job)
        assert job.status == PrintJob.Status.QUEUED
        assert job.attempts == 1
        assert job.next_attempt_at is not None

        # 2nd failure -> requeued, attempts=2.
        job.status = PrintJob.Status.PICKED
        job.agent = agent
        job.save(update_fields=["status", "agent"])
        job = _fail_once(job)
        assert job.status == PrintJob.Status.QUEUED
        assert job.attempts == 2

        # 3rd failure -> final failed.
        job.status = PrintJob.Status.PICKED
        job.agent = agent
        job.save(update_fields=["status", "agent"])
        job = _fail_once(job)
        assert job.status == PrintJob.Status.FAILED
        assert job.attempts == 3
        assert job.next_attempt_at is None


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
                PrintJob.objects.filter(pk=job.pk).update(status=PrintJob.Status.PICKED, agent=agent)
                services.update_job_status(agent=agent, job_id=job.pk, status="printing")
                services.update_job_status(agent=agent, job_id=job.pk, status="failed", error="x")

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
    with schema_context(tenant_a.schema_name):
        _seed_current_term()
        branch = BranchFactory()
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
            cohort_id=42,
        )
        assert job.status == PrintJob.Status.QUEUED


def test_quota_one_over_rejected(tenant_a, monkeypatch):
    from core.exceptions import StarforgeError

    with schema_context(tenant_a.schema_name):
        _seed_current_term()
        branch = BranchFactory()
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
                cohort_id=42,
            )
        assert exc.value.code == "print_quota_exceeded"


def test_quota_zero_never_blocks(tenant_a, monkeypatch):
    with schema_context(tenant_a.schema_name):
        _seed_current_term()
        branch = BranchFactory()
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
            cohort_id=42,
        )
        assert job.status == PrintJob.Status.QUEUED


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
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent1, _ = services.register_agent(branch_id=branch.pk, name="A1")
        agent2, _ = services.register_agent(branch_id=branch.pk, name="A2")
        agent1_id, agent2_id = agent1.pk, agent2.pk
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
        # Cleanup for transaction=True (no rollback).
        PrintJob.objects.all().delete()
        BranchAgent.objects.all().delete()
        connection.close()


# --------------------------------------------------------------------------- #
# Staff endpoints — perms + create (D4-LD-7)
# --------------------------------------------------------------------------- #
def test_staff_create_job_director(as_role, tenant_a):
    client, _user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = client.post(
        JOBS_URL,
        {
            "source": "report",
            "source_id": 5,
            "payload_s3_key": f"{tenant_a.schema_name}/r/5.pdf",
            "branch": branch.pk,
            "pages": 2,
            "copies": 1,
        },
        format="json",
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["status"] == "queued"


def test_create_job_rejects_foreign_tenant_payload_key(as_role, tenant_a):
    """R3/CONF2 (HIGH): the payload key is echoed into a presigned S3 GET at claim
    time; a key outside the caller's tenant prefix must be rejected so a staffer can't
    mint a download URL for another tenant's (or a cross-permission) object."""
    client, _user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = client.post(
        JOBS_URL,
        {
            "source": "report",
            "source_id": 5,
            "payload_s3_key": "tenant_b/finance/payroll.pdf",  # a foreign-tenant key
            "branch": branch.pk,
            "pages": 2,
        },
        format="json",
    )
    assert resp.status_code == 400, resp.content
    assert resp.json()["code"] == "validation_error"


def test_staff_create_job_teacher_allowed(as_role, tenant_a, user_in, as_user):
    # teacher has printing:write (request prints).
    teacher = user_in(tenant_a, roles=[Role.TEACHER])
    with schema_context(tenant_a.schema_name):
        branch_id = next(m.branch_id for m in teacher.role_memberships.all() if m.role == Role.TEACHER)
    client = as_user(tenant_a, teacher)
    resp = client.post(
        JOBS_URL,
        {
            "source": "assignment",
            "source_id": 6,
            "payload_s3_key": f"{tenant_a.schema_name}/a/6.pdf",
            "branch": branch_id,
            "pages": 1,
        },
        format="json",
    )
    assert resp.status_code == 201


def test_create_job_requires_owning_read_permission_for_the_source(tenant_a, user_in, as_user):
    """R4/PLAUS1: printing:write alone must not let a role pull a sensitive document it
    cannot otherwise read. A registrar holds printing:write but NOT academics:read, so a
    transcript print job (whose key is presign-downloaded at claim time) is forbidden."""
    registrar = user_in(tenant_a, roles=[Role.REGISTRAR])
    with schema_context(tenant_a.schema_name):
        branch_id = next(m.branch_id for m in registrar.role_memberships.all() if m.role == Role.REGISTRAR)
    resp = as_user(tenant_a, registrar).post(
        JOBS_URL,
        {
            "source": "transcript",
            "source_id": 1,
            "payload_s3_key": f"{tenant_a.schema_name}/transcripts/1.pdf",
            "branch": branch_id,
            "pages": 1,
        },
        format="json",
    )
    assert resp.status_code == 403, resp.content


@pytest.mark.parametrize("role", [Role.STUDENT, Role.PARENT])
def test_student_parent_cannot_create_job(as_role, tenant_a, role):
    client, _ = as_role(role)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = client.post(
        JOBS_URL,
        {"source": "report", "source_id": 9, "payload_s3_key": "x", "branch": branch.pk, "pages": 1},
        format="json",
    )
    assert resp.status_code == 403


def test_job_list_anonymous_denied(tenant_a, client_for):
    assert client_for(tenant_a).get(JOBS_URL).status_code == 401


@pytest.mark.parametrize("role", [Role.STUDENT, Role.PARENT])
def test_job_list_denied_roles(as_role, role):
    resp = as_role(role)[0].get(JOBS_URL)
    assert resp.status_code == 403


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
    keys = {row["payload_s3_key"] for row in client_b.get(JOBS_URL).json()["data"]}
    assert "a/secret.pdf" not in keys


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
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        for i in range(10):
            PrintJobFactory(branch=branch, source_id=200 + i)
    with django_assert_max_num_queries(10):
        resp = client.get(JOBS_URL)
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 10
