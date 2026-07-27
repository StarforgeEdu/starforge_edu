"""ORM-backed printing repositories (thin adapters over the preserved selectors)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.printing import selectors
from apps.printing.interfaces.repositories import (
    IBranchAgentRepository,
    IPrinterRepository,
    IPrintJobRepository,
)
from apps.printing.models import BranchAgent, Printer, PrintJob
from core.repositories import BaseRepository


class PrintJobRepository(BaseRepository[PrintJob], IPrintJobRepository):
    model = PrintJob

    def list_jobs(self) -> QuerySet[PrintJob]:
        return selectors.print_jobs()

    def get(self, *, pk: int) -> PrintJob | None:
        return selectors.print_jobs().filter(pk=pk).first()


class PrinterRepository(BaseRepository[Printer], IPrinterRepository):
    model = Printer

    def list_printers(self) -> QuerySet[Printer]:
        return selectors.printers()

    def get(self, *, pk: int) -> Printer | None:
        return selectors.printers().filter(pk=pk).first()

    def add(self, *, data: dict[str, Any]) -> Printer:
        return Printer.objects.create(**data)

    def apply_changes(self, printer: Printer, *, changes: dict[str, Any]) -> Printer:
        for field, value in changes.items():
            setattr(printer, field, value)
        if changes:
            printer.save(update_fields=[*changes.keys(), "updated_at"])
        return printer


class BranchAgentRepository(BaseRepository[BranchAgent], IBranchAgentRepository):
    model = BranchAgent

    def list_agents(self) -> QuerySet[BranchAgent]:
        return selectors.agents()

    def get(self, *, pk: int) -> BranchAgent | None:
        return selectors.agents().filter(pk=pk).first()
