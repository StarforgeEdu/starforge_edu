"""Staff rewards (F17-1) — a manager defines reward TYPES (cash / non-cash) and
GRANTS them to staff. A cash reward routes its money through the A-1 Approvals +
Ledger engine (a `reward`-kind ApprovalRequest), so every som paid out is signed
off and lands on the immutable ledger — the same anti-fraud spine as every other
money movement. Non-cash rewards (a day off, a certificate) are simply recorded.
"""

from __future__ import annotations

from django.db import models


class RewardType(models.Model):
    """A center-defined kind of reward (e.g. 'Performance bonus' cash, or
    'Extra day off' non-cash)."""

    name = models.CharField(max_length=120, unique=True)
    is_cash = models.BooleanField(default=False)
    default_amount_uzs = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}{' (cash)' if self.is_cash else ''}"


class RewardGrant(models.Model):
    reward_type = models.ForeignKey(RewardType, on_delete=models.PROTECT, related_name="grants")
    recipient = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="rewards_received")
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    granted_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # For a cash reward: the A-1 request that carries its approve→disburse→ledger flow.
    approval_request = models.ForeignKey(
        "approvals.ApprovalRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reward_grants",
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-granted_at",)
        indexes = [models.Index(fields=("recipient", "granted_at"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"reward:{self.reward_type_id}->user#{self.recipient_id}"
