"""Printing-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import factory
from django.utils import timezone

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

    @factory.lazy_attribute
    def agent(self):
        if self.status in (
            PrintJob.Status.PICKED,
            PrintJob.Status.PRINTING,
            PrintJob.Status.RECONCILIATION_REQUIRED,
        ):
            return BranchAgentFactory(branch=self.branch)
        return None

    @factory.lazy_attribute
    def lease_id(self):
        if self.status in (
            PrintJob.Status.PICKED,
            PrintJob.Status.PRINTING,
            PrintJob.Status.RECONCILIATION_REQUIRED,
        ):
            return uuid.uuid4()
        return None

    @factory.lazy_attribute
    def last_heartbeat_at(self):
        return timezone.now() if self.lease_id else None

    @factory.lazy_attribute
    def lease_expires_at(self):
        if not self.lease_id:
            return None
        delta = -1 if self.status == PrintJob.Status.RECONCILIATION_REQUIRED else 600
        return timezone.now() + timedelta(seconds=delta)

    @factory.lazy_attribute
    def reconciliation_required_at(self):
        return timezone.now() if self.status == PrintJob.Status.RECONCILIATION_REQUIRED else None

    @factory.lazy_attribute
    def reconciliation_reason(self):
        if self.status == PrintJob.Status.RECONCILIATION_REQUIRED:
            return PrintJob.ReconciliationReason.LEASE_EXPIRED
        return ""

    @factory.lazy_attribute
    def reconciliation_previous_status(self):
        if self.status == PrintJob.Status.RECONCILIATION_REQUIRED:
            return PrintJob.Status.PICKED
        return ""


def attach_trusted_assignment_files(
    *,
    schema: str,
    assignment: Any,
    filenames: list[str],
) -> list[str]:
    """Create canonical consumed-grant attachment fixtures for one assignment."""

    from apps.assignments.models import AssignmentUploadGrant
    from apps.assignments.storage_keys import final_attachment_key, pending_attachment_key

    keys: list[str] = []
    for index, filename in enumerate(filenames, start=1):
        pending = pending_attachment_key(
            schema=schema,
            owner_id=1,
            upload_id=f"{assignment.pk * 1_000 + index:032x}",
            filename=filename,
        )
        grant = AssignmentUploadGrant.objects.create(
            key=pending,
            content_type="application/pdf",
            expected_size_bytes=1,
            actual_size_bytes=1,
            expires_at=timezone.now() + timedelta(minutes=5),
            consumed_at=timezone.now(),
        )
        durable = final_attachment_key(
            schema=schema,
            target_kind="assignments",
            target_id=assignment.pk,
            grant_id=grant.pk,
            filename=filename,
        )
        grant.durable_key = durable
        grant.save(update_fields=["durable_key"])
        keys.append(durable)
    assignment.attachments = keys
    assignment.save(update_fields=["attachments"])
    return keys
