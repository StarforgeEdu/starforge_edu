"""Approvals + Ledger engine (A-1; PRODUCT_VISION themes #4/#15).

The generic spine for every "money movement that needs sign-off": a request is
created, approved/rejected, and (for money-moving kinds) disbursed by a cashier,
which writes an immutable LedgerEntry. Expenses, staff loans, procurement,
payment-delay, discount requests, salary-prep, event cost-split, book cash-sales,
and reward/points payouts are all configured KINDS of this one engine.

The ledger is append-only (no update/delete API), like the audit log — every som
that moves is one row, which is the "money can't disappear" anti-fraud guarantee.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class LedgerEntry(models.Model):
    class Direction(models.TextChoices):
        IN = "in", _("Money in")
        OUT = "out", _("Money out")

    direction = models.CharField(max_length=3, choices=Direction.choices, db_index=True)
    # Free-ish category (tuition/salary/expense/loan/procurement/book_sale/refund/reward/points...).
    entry_type = models.CharField(max_length=32, db_index=True)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="ledger_entries"
    )
    party_label = models.CharField(max_length=200, blank=True)  # who (free text in v1)
    payment_method = models.ForeignKey(
        "finance.PaymentMethod",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    # Soft reference to the originating object (no hard FK, so it works across apps).
    source_kind = models.CharField(max_length=40, blank=True)  # e.g. "approval_request"
    source_id = models.BigIntegerField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("entry_type", "created_at")),
            models.Index(fields=("source_kind", "source_id")),
            # The default unfiltered ledger:read list orders by created_at DESC; no existing
            # index leads with created_at (the composite leads with entry_type). LedgerEntry
            # is the append-only money spine, so serve the newest-first page from an index.
            models.Index(fields=("-created_at", "id"), name="ledger_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_uzs__gt=0), name="ledger_amount_positive"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"ledger#{self.pk}:{self.direction}:{self.entry_type}:{self.amount_uzs}"


class ApprovalRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        DISBURSED = "disbursed", _("Disbursed")
        CANCELLED = "cancelled", _("Cancelled")

    kind = models.CharField(max_length=32, db_index=True)  # expense/loan/procurement/discount/...
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="approval_requests"
    )
    requested_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, related_name="approval_requests"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    # Null amount = a decision-only request (e.g. discount / payment_delay) that never disburses.
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)  # kind-specific data
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    decided_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)
    disbursed_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    disbursed_at = models.DateTimeField(null=True, blank=True)
    payment_method = models.ForeignKey(
        "finance.PaymentMethod",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approval_requests",
    )
    ledger_entry = models.ForeignKey(
        LedgerEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name="approval_requests"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("kind", "status")),
            models.Index(fields=("requested_by", "status")),
            # The approver/cashier inbox lists newest-first, often unfiltered; neither
            # composite leads with created_at. ApprovalRequest is the money-movement spine
            # (all expense/loan/procurement/discount/reward requests) — index the sort.
            models.Index(fields=("-created_at", "id"), name="apprreq_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_uzs__isnull=True) | models.Q(amount_uzs__gt=0),
                name="approval_amount_positive_or_null",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"approval#{self.pk}:{self.kind}:{self.status}"
