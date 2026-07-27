"""Printing-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

import factory

from apps.org.tests.factories import BranchFactory
from apps.printing.models import BranchAgent, Printer, PrintJob
from core.utils import stable_hash


class PrinterFactory(factory.django.DjangoModelFactory[Printer]):
    class Meta:
        model = Printer
        django_get_or_create = ("branch", "name")

    branch = factory.SubFactory(BranchFactory)
    name = factory.Sequence(lambda n: f"Printer {n}")
    model_name = "HP LaserJet"
    capabilities = factory.LazyFunction(lambda: {"color": False, "duplex": True})


class BranchAgentFactory(factory.django.DjangoModelFactory[BranchAgent]):
    class Meta:
        model = BranchAgent

    branch = factory.SubFactory(BranchFactory)
    name = factory.Sequence(lambda n: f"Agent {n}")
    # Hash of a known raw token so tests can authenticate; the raw is never stored.
    token_hash = factory.Sequence(lambda n: stable_hash(f"raw-token-{n}"))


class PrintJobFactory(factory.django.DjangoModelFactory[PrintJob]):
    class Meta:
        model = PrintJob

    branch = factory.SubFactory(BranchFactory)
    status = PrintJob.Status.QUEUED
    source = PrintJob.Source.REPORT
    source_id = factory.Sequence(lambda n: n + 1)
    payload_s3_key = factory.Sequence(lambda n: f"tenant/reports/{n}.pdf")
    pages = 3
    copies = 1
