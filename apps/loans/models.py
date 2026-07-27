"""Staff loans (F21-1).

A staff loan is a `kind="loan"` of the A-1 Approvals + Ledger engine: it is
requested, approved, and disbursed (money OUT -> immutable LedgerEntry) through
`apps.approvals`. What makes a loan a *loan* rather than a one-off expense is that
it must be paid BACK — so this app adds repayment tracking on top of the engine.

Each `LoanRepayment` records money coming IN against the disbursed loan and writes
its own LedgerEntry. The loan's outstanding balance is `disbursed amount - sum of
repayments`; when it reaches zero the loan is settled. Nothing here re-implements
the request/approve/disburse state machine - that stays in the one engine.
"""

from __future__ import annotations

from django.db import models


class LoanRepayment(models.Model):
    # The loan being repaid is an approvals.ApprovalRequest (kind="loan"). PROTECT:
    # a loan with recorded repayments can never be silently deleted out from under
    # its money trail.
    loan = models.ForeignKey(
        "approvals.ApprovalRequest", on_delete=models.PROTECT, related_name="loan_repayments"
    )
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="loan_repayments"
    )
    payment_method = models.ForeignKey(
        "finance.PaymentMethod",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="loan_repayments",
    )
    # The money-IN row this repayment wrote (the audit link back into the ledger).
    ledger_entry = models.ForeignKey(
        "approvals.LedgerEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    recorded_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, related_name="+")
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("loan", "created_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_uzs__gt=0), name="loan_repayment_amount_positive"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"repayment#{self.pk}:loan#{self.loan_id}:{self.amount_uzs}"
