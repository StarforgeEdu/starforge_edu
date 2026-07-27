"""Printing-domain repository ports."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.printing.models import BranchAgent, Printer, PrintJob
from core.interfaces import IBaseRepository


class IPrintJobRepository(IBaseRepository[PrintJob]):
    def list_jobs(self) -> QuerySet[PrintJob]:
        raise NotImplementedError

    def get(self, *, pk: int) -> PrintJob | None:
        raise NotImplementedError


class IPrinterRepository(IBaseRepository[Printer]):
    def list_printers(self) -> QuerySet[Printer]:
        raise NotImplementedError

    def get(self, *, pk: int) -> Printer | None:
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]) -> Printer:
        raise NotImplementedError

    def apply_changes(self, printer: Printer, *, changes: dict[str, Any]) -> Printer:
        raise NotImplementedError


class IBranchAgentRepository(IBaseRepository[BranchAgent]):
    def list_agents(self) -> QuerySet[BranchAgent]:
        raise NotImplementedError

    def get(self, *, pk: int) -> BranchAgent | None:
        raise NotImplementedError
