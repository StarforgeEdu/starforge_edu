"""Printing services — orchestration over the preserved domain fns
(enqueue_print/register_agent/revoke_agent/claim_job/update_job_status)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db.models import QuerySet

from apps.printing import services as domain
from apps.printing.interfaces.repositories import (
    IBranchAgentRepository,
    IPrinterRepository,
    IPrintJobRepository,
)
from apps.printing.interfaces.services import (
    IBranchAgentService,
    IPrinterService,
    IPrintJobService,
)
from apps.printing.models import BranchAgent, Printer, PrintJob, PrintJobReconciliation


class PrintJobService(IPrintJobService):
    def __init__(self, repository: IPrintJobRepository) -> None:
        self.repository = repository

    def list_jobs(self) -> QuerySet[PrintJob]:
        return self.repository.list_jobs()

    def get(self, *, pk: int) -> PrintJob | None:
        return self.repository.get(pk=pk)

    def enqueue(self, *, data: dict[str, Any], requested_by) -> PrintJob:
        return domain.enqueue_print(
            source=data["source"],
            source_id=data["source_id"],
            payload_s3_key=data["payload_s3_key"],
            branch_id=data["branch"],
            requested_by=requested_by,
            pages=data["pages"],
            copies=data["copies"],
            color=data["color"],
            duplex=data["duplex"],
            cohort_id=data["cohort"],
        )

    def claim(self, *, agent: BranchAgent) -> PrintJob | None:
        return domain.claim_job(agent=agent)

    def reject_invalid_claim(self, *, agent: BranchAgent, job_id: int) -> PrintJob:
        return domain.reject_invalid_claim(agent=agent, job_id=job_id)

    def heartbeat(
        self,
        *,
        agent: BranchAgent,
        job_id: int,
        lease_id: UUID,
        pages_printed: int | None,
    ) -> PrintJob:
        return domain.heartbeat_job(
            agent=agent,
            job_id=job_id,
            lease_id=lease_id,
            pages_printed=pages_printed,
        )

    def update_status(
        self,
        *,
        agent: BranchAgent,
        job_id: int,
        lease_id: UUID,
        status: str,
        error: str,
        pages_printed: int | None,
    ) -> PrintJob:
        return domain.update_job_status(
            agent=agent,
            job_id=job_id,
            lease_id=lease_id,
            status=status,
            error=error,
            pages_printed=pages_printed,
        )

    def list_reconciliations(self, *, job_id: int) -> QuerySet[PrintJobReconciliation]:
        return self.repository.list_reconciliations(job_id=job_id)

    def reconcile(
        self,
        *,
        job_id: int,
        expected_branch_id: int,
        actor,
        actor_principal,
        outcome: str,
        evidence_reference: str,
        idempotency_key: str,
    ) -> PrintJob:
        return domain.reconcile_print_job(
            job_id=job_id,
            expected_branch_id=expected_branch_id,
            actor=actor,
            actor_principal=actor_principal,
            outcome=outcome,
            evidence_reference=evidence_reference,
            idempotency_key=idempotency_key,
        )


class PrinterService(IPrinterService):
    def __init__(self, repository: IPrinterRepository) -> None:
        self.repository = repository

    def list_printers(self) -> QuerySet[Printer]:
        return self.repository.list_printers()

    def get(self, *, pk: int) -> Printer | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any]) -> Printer:
        return self.repository.add(data=data)

    def update(self, printer: Printer, changes: dict[str, Any]) -> Printer:
        return self.repository.apply_changes(printer, changes=changes)


class BranchAgentService(IBranchAgentService):
    def __init__(self, repository: IBranchAgentRepository) -> None:
        self.repository = repository

    def list_agents(self) -> QuerySet[BranchAgent]:
        return self.repository.list_agents()

    def get(self, *, pk: int) -> BranchAgent | None:
        return self.repository.get(pk=pk)

    def register(self, *, branch_id: int, name: str, created_by) -> tuple[BranchAgent, str]:
        return domain.register_agent(branch_id=branch_id, name=name, created_by=created_by)

    def revoke(self, agent: BranchAgent) -> BranchAgent:
        return domain.revoke_agent(agent_id=agent.pk)
