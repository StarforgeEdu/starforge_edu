"""Procurement / purchase orders (#15) — a `kind="procurement"` of the A-1 Approvals
+ Ledger engine.

A purchase order is itemised: WHAT is being bought (line items, qty x unit price)
from WHICH supplier, totalling the amount on the underlying ApprovalRequest. The
request → approve → cashier disburses (money OUT → immutable LedgerEntry, named to
the supplier) lifecycle is the one engine; this app only adds the itemised PO so the
centre has an exact record of every som spent and on what (the anti-fraud trail).
"""

from __future__ import annotations

from django.db import models


class PurchaseOrder(models.Model):
    # The decision/disbursement lives on this ApprovalRequest (kind="procurement");
    # PROTECT so a PO can never be orphaned from its money trail.
    request = models.OneToOneField(
        "approvals.ApprovalRequest", on_delete=models.PROTECT, related_name="purchase_order"
    )
    supplier = models.CharField(max_length=200)
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="purchase_orders"
    )
    created_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("branch", "created_at")),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"po#{self.pk}:{self.supplier}"


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    unit_price_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="po_item_quantity_positive"),
            models.CheckConstraint(
                condition=models.Q(unit_price_uzs__gte=0), name="po_item_unit_price_nonneg"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"po_item#{self.pk}:{self.description}"

    @property
    def line_total_uzs(self):
        return self.quantity * self.unit_price_uzs
