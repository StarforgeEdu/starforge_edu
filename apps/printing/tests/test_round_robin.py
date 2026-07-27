"""F16-1 — even job distribution across a branch's printers: on claim, a job is
balanced onto the least-loaded active printer (round-robin), so one printer isn't
swamped while the rest sit idle."""

from __future__ import annotations

from collections import Counter

import pytest
from django.db import connection
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.printing import services

pytestmark = pytest.mark.django_db


def _branch_with_agent(tenant):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory()
        agent, _ = services.register_agent(branch_id=branch.pk, name="agent")
    return branch, agent


def _queue(branch, n):
    from apps.printing.tests.factories import PrintJobFactory

    for i in range(n):
        PrintJobFactory(branch=branch, source_id=1000 + i, next_attempt_at=timezone.now())


def test_claim_round_robins_across_active_printers(tenant_a):
    from apps.printing.tests.factories import PrinterFactory

    branch, agent = _branch_with_agent(tenant_a)
    with schema_context(tenant_a.schema_name):
        printer_a = PrinterFactory(branch=branch, name="A")
        printer_b = PrinterFactory(branch=branch, name="B")
        _queue(branch, 4)
        assigned = [services.claim_job(agent=agent).printer_id for _ in range(4)]

    counts = Counter(assigned)
    assert counts[printer_a.id] == 2  # 4 jobs spread evenly...
    assert counts[printer_b.id] == 2  # ...2 to each printer, not 4 to one


def test_no_printers_leaves_job_unassigned(tenant_a):
    branch, agent = _branch_with_agent(tenant_a)
    with schema_context(tenant_a.schema_name):
        _queue(branch, 1)
        job = services.claim_job(agent=agent)
        assert job.printer_id is None  # the agent falls back to its own default device


def test_inactive_printer_is_skipped(tenant_a):
    from apps.printing.tests.factories import PrinterFactory

    branch, agent = _branch_with_agent(tenant_a)
    with schema_context(tenant_a.schema_name):
        active = PrinterFactory(branch=branch, name="A", is_active=True)
        PrinterFactory(branch=branch, name="B", is_active=False)
        _queue(branch, 2)
        assigned = [services.claim_job(agent=agent).printer_id for _ in range(2)]

    assert all(printer_id == active.id for printer_id in assigned)  # only the active one


def test_failed_retry_clears_printer_to_rebalance(tenant_a):
    from apps.printing.models import PrintJob
    from apps.printing.tests.factories import PrinterFactory

    branch, agent = _branch_with_agent(tenant_a)
    with schema_context(tenant_a.schema_name):
        PrinterFactory(branch=branch, name="A")
        _queue(branch, 1)
        job = services.claim_job(agent=agent)
        assert job.printer_id is not None  # assigned on first claim
        # the agent reports a (printer-specific) failure -> requeued for a fresh attempt
        job = services.update_job_status(
            agent=agent, job_id=job.pk, status=PrintJob.Status.FAILED, error="paper jam"
        )
        assert job.status == PrintJob.Status.QUEUED
        assert job.printer_id is None  # NOT pinned to the failed printer; next claim rebalances


@pytest.mark.django_db(transaction=True)
def test_claim_works_under_real_autocommit(tenant_a):
    """Regression guard for the select_for_update-outside-tx class: claim_job uses
    select_for_update, so it MUST keep its own @transaction.atomic. Under real
    autocommit (transaction=True — no test-supplied ambient transaction), a missing
    decorator raises TransactionManagementError; the plain-django_db tests above would
    NOT catch that because they run inside an ambient transaction."""
    from apps.org.tests.factories import BranchFactory
    from apps.printing.models import BranchAgent, Printer, PrintJob
    from apps.printing.tests.factories import PrinterFactory, PrintJobFactory

    with schema_context(tenant_a.schema_name):
        try:
            branch = BranchFactory()
            agent, _ = services.register_agent(branch_id=branch.pk, name="agent")
            PrinterFactory(branch=branch, name="A")
            PrintJobFactory(branch=branch, source_id=7, next_attempt_at=timezone.now())
            job = services.claim_job(agent=agent)  # must NOT raise outside-a-transaction
            assert job is not None
            assert job.status == PrintJob.Status.PICKED
            assert job.printer_id is not None
        finally:  # transaction=True doesn't roll back — clean up explicitly
            PrintJob.objects.all().delete()
            Printer.objects.all().delete()
            BranchAgent.objects.all().delete()
            connection.close()
