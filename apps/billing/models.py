"""Billing models — TD-8 platform monetization (PUBLIC schema only).

These tables live exclusively in the public schema (apps.billing is in
SHARED_APPS, NOT TENANT_APPS). One Plan catalog platform-wide; one
Subscription per Center; one UsageSnapshot per (Center, day).

Cross-schema note: a tenant-schema `post_save` receiver cannot audit
Subscription (it is a public-schema row). Lane E writes subscription audit
entries explicitly via `audit_log()` inside `schema_context(center.schema_name)`
from its services (see Lane D decision in DAY-3.md).
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Plan(models.Model):
    """A subscription tier (TD-8 field list, verbatim). `[OWNER:O-12]` real pricing."""

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    max_students = models.PositiveIntegerField()
    max_branches = models.PositiveIntegerField()
    ai_tokens_month = models.BigIntegerField()
    storage_gb = models.PositiveIntegerField()
    price_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    # F9-2: metered AI overage — UZS charged per 1000 AI tokens used BEYOND
    # ai_tokens_month. 0 (default) means AI overage is not billed on this plan.
    ai_overage_price_per_1k_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("price_uzs",)
        constraints = [
            models.CheckConstraint(condition=models.Q(price_uzs__gte=0), name="plan_price_non_negative"),
            models.CheckConstraint(
                condition=models.Q(ai_overage_price_per_1k_uzs__gte=0),
                name="plan_ai_overage_price_non_negative",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code} ({self.price_uzs} UZS)"


class Subscription(models.Model):
    """One Center's subscription state.

    "Expired" is represented as `suspended` — there is no separate expired
    status; the nightly metering task flips trialing/active/past_due → suspended.
    """

    class Status(models.TextChoices):
        TRIALING = "trialing", _("Trialing")
        ACTIVE = "active", _("Active")
        PAST_DUE = "past_due", _("Past due")
        SUSPENDED = "suspended", _("Suspended")

    center = models.OneToOneField("tenancy.Center", on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.TRIALING, db_index=True)
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("center_id",)
        indexes = [
            models.Index(fields=("status", "current_period_end"), name="billing_sub_status_end_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.center_id}:{self.status}"


class UsageSnapshot(models.Model):
    """Daily usage meter per Center (one row per (center, date)). Re-running the
    nightly task updates the row, never duplicates it."""

    center = models.ForeignKey("tenancy.Center", on_delete=models.CASCADE, related_name="usage_snapshots")
    date = models.DateField()
    students_count = models.PositiveIntegerField(default=0)
    storage_bytes = models.BigIntegerField(default=0)
    ai_tokens_used = models.BigIntegerField(default=0)
    dau = models.PositiveIntegerField(default=0)  # daily active users captured at snapshot time
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-date",)
        constraints = [
            models.UniqueConstraint(fields=("center", "date"), name="usage_one_snapshot_per_center_day"),
        ]
        indexes = [models.Index(fields=("center", "date"), name="billing_usage_center_date_idx")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.center_id}@{self.date}"


class AiUsageCharge(models.Model):
    """Metered AI-overage charge per (Center, billing month) — F9-2.

    AI generation beyond the plan's ``ai_tokens_month`` allowance is charged per use:
    ``amount_uzs = overage_tokens / 1000 * plan.ai_overage_price_per_1k_uzs``. One row
    per (center, period) — the nightly meter re-computes it in place as the month
    accrues, so it is month-to-date until the period closes (never duplicated).

    ``cost_microusd`` records the platform's underlying provider cost for the month
    (summed ``AIRequest.cost_microusd``) so platform staff can reconcile billed
    revenue against real cost; it is NOT the billed amount.
    """

    center = models.ForeignKey("tenancy.Center", on_delete=models.CASCADE, related_name="ai_usage_charges")
    period = models.DateField(help_text="First day of the billing month this charge covers.")
    included_tokens = models.BigIntegerField(default=0)  # the plan allowance at metering time
    used_tokens = models.BigIntegerField(default=0)  # tokens consumed this month
    overage_tokens = models.BigIntegerField(default=0)  # max(0, used - included)
    rate_per_1k_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cost_microusd = models.BigIntegerField(default=0)  # underlying provider cost (reconciliation)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-period", "center_id")
        constraints = [
            # The unique (center, period) index this constraint creates also serves the
            # by-center / by-(center,period) lookups, so no separate Index is needed.
            models.UniqueConstraint(fields=("center", "period"), name="ai_charge_one_per_center_period"),
            models.CheckConstraint(condition=models.Q(amount_uzs__gte=0), name="ai_charge_amount_non_negative"),
            models.CheckConstraint(
                condition=models.Q(overage_tokens__gte=0), name="ai_charge_overage_non_negative"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.center_id}@{self.period}:{self.amount_uzs}"
