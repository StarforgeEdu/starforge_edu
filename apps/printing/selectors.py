"""Printing read-side selectors (D4-LD).

Presenters intentionally use foreign-key ids and never dereference related identity,
storage, or device rows. Avoid eager joins here: they add work to every paginated
register query and load sensitive objects that the response contract does not use.
Exact branch visibility is applied by the view before pagination.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.printing.models import BranchAgent, Printer, PrintJob, PrintJobReconciliation


def print_jobs() -> QuerySet[PrintJob]:
    return PrintJob.objects.all()


def print_job_reconciliations(*, job_id: int) -> QuerySet[PrintJobReconciliation]:
    return PrintJobReconciliation.objects.filter(job_id=job_id)


def printers() -> QuerySet[Printer]:
    return Printer.objects.all()


def agents() -> QuerySet[BranchAgent]:
    return BranchAgent.objects.all()
