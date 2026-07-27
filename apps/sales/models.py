"""Book / material cash sales (#8 — `book_cash`).

A point-of-sale counterpart to the A-1 money-OUT kinds: a student buys a book,
uniform, or material for cash, and the takings are recorded as an immutable money-IN
`LedgerEntry` — so incoming cash is just as un-loseable as outgoing (the anti-fraud
moat covers both directions). A sale is recorded directly (no approval needed), and a
refund writes a compensating money-OUT row rather than mutating the original — the
ledger stays append-only. The student names the party on both rows for reconciliation.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Sale(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "completed", _("Completed")
        REFUNDED = "refunded", _("Refunded")

    item = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField(default=1)
    unit_price_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)  # quantity x unit price
    student = models.ForeignKey("students.StudentProfile", on_delete=models.PROTECT, related_name="purchases")
    # Denormalized from the student, for branch-scoped visibility / the till's books.
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="sales"
    )
    payment_method = models.ForeignKey(
        "finance.PaymentMethod", on_delete=models.PROTECT, null=True, blank=True, related_name="sales"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COMPLETED, db_index=True)
    # The money-IN row this sale wrote, and (on refund) the compensating money-OUT row.
    ledger_entry = models.ForeignKey(
        "approvals.LedgerEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    refund_ledger_entry = models.ForeignKey(
        "approvals.LedgerEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    sold_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, related_name="+")
    refunded_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    refunded_at = models.DateTimeField(null=True, blank=True)
    refund_reason = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("branch", "status")),
            models.Index(fields=("student", "created_at")),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="sale_quantity_positive"),
            models.CheckConstraint(condition=models.Q(amount_uzs__gt=0), name="sale_amount_positive"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"sale#{self.pk}:{self.item}:{self.amount_uzs}:{self.status}"
