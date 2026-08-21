"""Printing-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from django.db.models import QuerySet

from apps.printing.models import BranchAgent, Printer, PrintJob, PrintJobReconciliation


class IPrintJobService(ABC):
    @abstractmethod
    def list_jobs(self) -> QuerySet[PrintJob]: ...

    @abstractmethod
    def get(self, *, pk: int) -> PrintJob | None: ...

    @abstractmethod
    def enqueue(self, *, data: dict[str, Any], requested_by) -> PrintJob: ...

    @abstractmethod
    def request_upload(self, *, data: dict[str, Any], requested_by) -> dict[str, Any]: ...

    @abstractmethod
    def enqueue_upload(self, *, data: dict[str, Any], requested_by) -> PrintJob: ...

    @abstractmethod
    def claim(self, *, agent: BranchAgent) -> PrintJob | None: ...

    @abstractmethod
    def reject_invalid_claim(self, *, agent: BranchAgent, job_id: int) -> PrintJob: ...

    @abstractmethod
    def heartbeat(
        self,
        *,
        agent: BranchAgent,
        job_id: int,
        lease_id: UUID,
        pages_printed: int | None,
    ) -> PrintJob: ...

    @abstractmethod
    def update_status(
        self,
        *,
        agent: BranchAgent,
        job_id: int,
        lease_id: UUID,
        status: str,
        error: str,
        pages_printed: int | None,
    ) -> PrintJob: ...

    @abstractmethod
    def list_reconciliations(self, *, job_id: int) -> QuerySet[PrintJobReconciliation]: ...

    @abstractmethod
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
    ) -> PrintJob: ...


class IPrinterService(ABC):
    @abstractmethod
    def list_printers(self) -> QuerySet[Printer]: ...

    @abstractmethod
    def get(self, *, pk: int) -> Printer | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any]) -> Printer: ...

    @abstractmethod
    def update(self, printer: Printer, changes: dict[str, Any]) -> Printer: ...


class IBranchAgentService(ABC):
    @abstractmethod
    def list_agents(self) -> QuerySet[BranchAgent]: ...

    @abstractmethod
    def get(self, *, pk: int) -> BranchAgent | None: ...

    @abstractmethod
    def register(self, *, branch_id: int, name: str, created_by) -> tuple[BranchAgent, str]: ...

    @abstractmethod
    def revoke(self, agent: BranchAgent) -> BranchAgent: ...
