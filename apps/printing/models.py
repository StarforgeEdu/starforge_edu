"""Printing (server side) models — D4-LD-1.

The print pipeline is pull-based (ADR-004, TASKS §28): a *branch agent* (a
separate repo / deploy target — NO CUPS code here) authenticates with a hashed
token, claims the oldest queued ``PrintJob`` for its branch, downloads the
payload from S3 via a presigned URL, prints it, then reports status back. Jobs
are created by ``apps.printing.services.enqueue_print`` (called by transcripts,
receipts, reports — and the staff ``POST /printing/jobs/`` path).
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Printer(models.Model):
    """A physical printer attached to a branch (registered by staff)."""

    branch = models.ForeignKey("org.Branch", on_delete=models.CASCADE, related_name="printers")
    name = models.CharField(_("name"), max_length=120)
    model_name = models.CharField(_("model name"), max_length=120, blank=True)
    # e.g. {"color": true, "duplex": true, "paper": ["A4", "A5"]}
    capabilities = models.JSONField(_("capabilities"), default=dict, blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("branch", "name")
        constraints = [
            models.UniqueConstraint(fields=("branch", "name"), name="printer_unique_branch_name"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.branch_id}:{self.name}"


class BranchAgent(models.Model):
    """A trusted branch-side daemon that polls + prints queued jobs.

    Authenticates via ``Authorization: Agent <raw-token>`` — only the sha256
    ``token_hash`` is stored (the raw token is shown once at registration and
    never persisted). ``revoked_at`` set => the token no longer authenticates.
    """

    branch = models.ForeignKey("org.Branch", on_delete=models.CASCADE, related_name="print_agents")
    name = models.CharField(_("name"), max_length=120)
    token_hash = models.CharField(_("token hash"), max_length=64, unique=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    last_seen_at = models.DateTimeField(_("last seen at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("branch", "name")
        indexes = [models.Index(fields=("token_hash",), name="printing_agent_token_idx")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.branch_id}:{self.name}"

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class PrintJob(models.Model):
    """One document to print, pulled by a branch agent. Pull-based, never pushed."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        PICKED = "picked", _("Picked")
        PRINTING = "printing", _("Printing")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    class Source(models.TextChoices):
        ASSIGNMENT = "assignment", _("Assignment")
        TRANSCRIPT = "transcript", _("Transcript")
        REPORT = "report", _("Report")
        RECEIPT = "receipt", _("Receipt")

    branch = models.ForeignKey("org.Branch", on_delete=models.CASCADE, related_name="print_jobs")
    printer = models.ForeignKey(
        Printer, on_delete=models.SET_NULL, null=True, blank=True, related_name="print_jobs"
    )
    agent = models.ForeignKey(
        BranchAgent, on_delete=models.SET_NULL, null=True, blank=True, related_name="print_jobs"
    )
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    source = models.CharField(_("source"), max_length=16, choices=Source.choices)
    source_id = models.PositiveBigIntegerField(_("source id"))
    payload_s3_key = models.CharField(_("payload S3 key"), max_length=512)
    pages = models.PositiveIntegerField(_("pages"))
    copies = models.PositiveSmallIntegerField(_("copies"), default=1)
    color = models.BooleanField(_("color"), default=False)
    duplex = models.BooleanField(_("duplex"), default=False)
    # No FK: used only for per-cohort/term quota lookups (the cohort may live in
    # a different lane's app and the job survives its deletion).
    cohort_id = models.PositiveBigIntegerField(_("cohort id"), null=True, blank=True)
    requested_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="print_jobs"
    )
    attempts = models.PositiveSmallIntegerField(_("attempts"), default=0)
    next_attempt_at = models.DateTimeField(_("next attempt at"), null=True, blank=True, db_index=True)
    pages_printed = models.PositiveIntegerField(_("pages printed"), default=0)
    last_error = models.TextField(_("last error"), blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(_("claimed at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("branch", "source", "source_id", "payload_s3_key"),
                condition=models.Q(status__in=("queued", "picked", "printing")),
                name="printing_unique_open_source",
            ),
        ]
        indexes = [
            models.Index(fields=("branch", "status", "next_attempt_at"), name="printing_job_claim_idx"),
            models.Index(fields=("source", "source_id"), name="printing_job_source_idx"),
            # The whole-tenant jobs list is newest-first; no existing index leads with
            # created_at. Print jobs accumulate fast (one per printed doc) — index the sort
            # (mirrors AIRequest, which already indexes its -created_at default ordering).
            models.Index(fields=("-created_at", "id"), name="printjob_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"PrintJob#{self.pk}:{self.source}:{self.status}"
