"""Printing read-side selectors (D4-LD).

All non-trivial reads live here with eager loading. Staff visibility is the
whole tenant (jobs/printers/agents are operational data, not per-user PII);
branch object-scoping for non-director staff is enforced by the viewset's
``object_scope``.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.printing.models import BranchAgent, Printer, PrintJob


def print_jobs() -> QuerySet[PrintJob]:
    return PrintJob.objects.select_related("branch", "printer", "agent", "requested_by")


def printers() -> QuerySet[Printer]:
    return Printer.objects.select_related("branch")


def agents() -> QuerySet[BranchAgent]:
    return BranchAgent.objects.select_related("branch", "created_by")
