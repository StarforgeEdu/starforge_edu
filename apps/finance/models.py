"""Finance domain models (TASKS §15, D3-A-1).

The billing ledger: fee schedules, invoices (FX-snapshotted), invoice lines,
discounts/scholarships, payment plans, payment allocations, refunds, cashier
shifts. All live in the tenant schema. Money is `Decimal(18, 2)` in UZS.

Cross-lane decision: `PaymentAllocation.payment_id` and `Refund.payment_id` are
plain `BigIntegerField` SOFT references to `payments.Payment` (NOT FKs) so this
lane's migration never depends on Lane B's same-day migration (merge order
A -> B). Lane B's `Payment` may FK `finance.CashierShift` because B merges after.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.utils.translation import gettext_lazy as _

# Money columns are uniformly Decimal(18, 2) in UZS (DAY-3 Lane A spec).


class FeeSchedule(models.Model):
    """A recurring or one-off charge template. `cohort=None` is a center-wide
    default applied to any student lacking a cohort-specific schedule."""

    class BillingPeriod(models.TextChoices):
        MONTHLY = "monthly", _("Monthly")
        TERM = "term", _("Term")
        ONE_TIME = "one_time", _("One-time")

    name = models.CharField(max_length=120)
    cohort = models.ForeignKey(
        "cohorts.Cohort",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="fee_schedules",
    )
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    billing_period = models.CharField(
        max_length=10, choices=BillingPeriod.choices, default=BillingPeriod.MONTHLY
    )
    due_day_of_month = models.PositiveSmallIntegerField(default=5)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)
        indexes = [models.Index(fields=("is_active", "cohort"))]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_uzs__gte=Decimal("0")),
                name="fee_schedule_amount_non_negative",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.amount_uzs} UZS / {self.billing_period})"


class Invoice(models.Model):
    """A bill issued to one student. `number` is `INV-{YYYY}-{seq:06d}` per
    center, unique. FX is snapshotted at issue time (`fx_rate_usd`/`total_usd`
    frozen) so a later rate move never restates a historical invoice."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ISSUED = "issued", _("Issued")
        PARTIALLY_PAID = "partially_paid", _("Partially paid")
        PAID = "paid", _("Paid")
        VOID = "void", _("Void")
        OVERDUE = "overdue", _("Overdue")

    number = models.CharField(max_length=32, unique=True)
    student = models.ForeignKey("students.StudentProfile", on_delete=models.PROTECT, related_name="invoices")
    cohort = models.ForeignKey(
        "cohorts.Cohort",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
    )
    fee_schedule = models.ForeignKey(
        FeeSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
    )
    period = models.CharField(
        max_length=16,
        blank=True,
        help_text=_("Billing period key (e.g. '2026-06') for enrollment dedupe."),
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True, db_index=True)
    currency = models.CharField(max_length=3, default="UZS")
    total_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    # FX snapshot frozen at issue. fx_source is recorded for audit/repro.
    fx_rate_usd = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    fx_source = models.CharField(max_length=32, blank=True)
    total_usd = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("student", "status")),
            models.Index(fields=("status", "due_date")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_uzs__gte=Decimal("0")),
                name="invoice_total_non_negative",
            ),
            # Dedupe target for auto-issue-on-enrollment (D3-A-3): one invoice per
            # (student, fee_schedule, period) when a fee_schedule + period are set.
            models.UniqueConstraint(
                fields=("student", "fee_schedule", "period"),
                condition=~models.Q(period="") & models.Q(fee_schedule__isnull=False),
                name="invoice_one_per_student_schedule_period",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.number


class InvoiceLine(models.Model):
    """One charge or discount line on an invoice. A negative `amount_uzs` is only
    valid for `line_type=discount` (CheckConstraint)."""

    class LineType(models.TextChoices):
        TUITION = "tuition", _("Tuition")
        MATERIAL = "material", _("Material")
        PENALTY = "penalty", _("Penalty")
        DISCOUNT = "discount", _("Discount")
        OTHER = "other", _("Other")

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=255)
    line_type = models.CharField(max_length=10, choices=LineType.choices, default=LineType.TUITION)
    quantity = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("1"))
    unit_price_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(
                # Negative line totals are only legal for discounts.
                condition=models.Q(amount_uzs__gte=Decimal("0")) | models.Q(line_type="discount"),
                name="invoice_line_negative_only_discount",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.invoice_id}:{self.description}"


class Discount(models.Model):
    """A standing student discount (sibling / scholarship / manual). Exactly one
    of `percent` / `fixed_amount_uzs` is set (CheckConstraint). Materializes as a
    negative discount `InvoiceLine` at issue time."""

    class DiscountType(models.TextChoices):
        SIBLING = "sibling", _("Sibling")
        SCHOLARSHIP = "scholarship", _("Scholarship")
        MANUAL = "manual", _("Manual")

    student = models.ForeignKey("students.StudentProfile", on_delete=models.CASCADE, related_name="discounts")
    discount_type = models.CharField(max_length=12, choices=DiscountType.choices)
    percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    fixed_amount_uzs = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    is_active = models.BooleanField(default=True, db_index=True)
    # F23-1: a one-time credit (e.g. an absence deduction) — retires (is_active=False)
    # the first time it actually applies to an invoice, so it credits exactly one bill
    # instead of recurring on every future invoice like a standing scholarship.
    single_use = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("student", "is_active"))]
        constraints = [
            models.CheckConstraint(
                # XOR: exactly one of percent / fixed_amount_uzs must be set.
                condition=(
                    models.Q(percent__isnull=False, fixed_amount_uzs__isnull=True)
                    | models.Q(percent__isnull=True, fixed_amount_uzs__isnull=False)
                ),
                name="discount_exactly_one_of_percent_or_fixed",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.student_id}:{self.discount_type}"


class PaymentPlan(models.Model):
    """An installment plan for a single invoice. Installments must sum to
    `invoice.total_uzs` (validated in the service)."""

    invoice = models.OneToOneField(Invoice, on_delete=models.CASCADE, related_name="payment_plan")
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover
        return f"plan:{self.invoice_id}"


class PaymentPlanInstallment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        PAID = "paid", _("Paid")
        OVERDUE = "overdue", _("Overdue")

    plan = models.ForeignKey(PaymentPlan, on_delete=models.CASCADE, related_name="installments")
    due_date = models.DateField()
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("due_date",)
        indexes = [models.Index(fields=("plan", "status"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.plan_id}:{self.due_date}:{self.amount_uzs}"


class PaymentAllocation(models.Model):
    """Links a portion of a payment to an invoice. `payment_id` is a SOFT
    reference (BigInteger, not a FK) to `payments.Payment` — see module
    docstring for the cross-lane FK-avoidance decision."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="allocations")
    payment_id = models.BigIntegerField(db_index=True)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("payment_id",))]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_uzs__gt=Decimal("0")),
                name="allocation_amount_positive",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"pay#{self.payment_id}->inv#{self.invoice_id}:{self.amount_uzs}"


class Refund(models.Model):
    """A refund against an invoice, driven through a state machine. `payment_id`
    is a SOFT reference (BigInteger, not a FK) to `payments.Payment`."""

    class State(models.TextChoices):
        REQUESTED = "requested", _("Requested")
        APPROVED = "approved", _("Approved")
        SENT_TO_PROVIDER = "sent_to_provider", _("Sent to provider")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="refunds")
    payment_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    reason = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.REQUESTED, db_index=True)
    requested_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("invoice", "state"))]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_uzs__gt=Decimal("0")),
                name="refund_amount_positive",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"refund#{self.pk}:inv#{self.invoice_id}:{self.state}"


class CashierShift(models.Model):
    """A cashier's cash drawer session. Only one open shift per cashier at a time
    (enforced in the service). `discrepancy_uzs = closing_cash - (opening_cash +
    cash payments in shift)` computed at close."""

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        CLOSED = "closed", _("Closed")

    cashier = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="cashier_shifts")
    branch = models.ForeignKey("org.Branch", on_delete=models.PROTECT, related_name="cashier_shifts")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    opening_cash_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    closing_cash_uzs = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    discrepancy_uzs = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-opened_at",)
        indexes = [models.Index(fields=("cashier", "status"))]
        constraints = [
            # At most one OPEN shift per cashier (DB-level backstop for the
            # service guard; partial unique on status='open').
            models.UniqueConstraint(
                fields=("cashier",),
                condition=models.Q(status="open"),
                name="one_open_shift_per_cashier",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"shift#{self.pk}:{self.cashier_id}:{self.status}"


class PaymentMethod(models.Model):
    """Dynamic disbursement/receipt method (F14): cash, card, bank transfer, …
    Admin-managed per Center so it fits any center's accounting."""

    name = models.CharField(max_length=64)
    slug = models.SlugField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class Expense(models.Model):
    """A center expense: created -> approved -> paid (or rejected). The money is
    disbursed in a chosen PaymentMethod on the pay step (F14-1)."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending approval")
        APPROVED = "approved", _("Approved")
        PAID = "paid", _("Paid")
        REJECTED = "rejected", _("Rejected")

    branch = models.ForeignKey("org.Branch", on_delete=models.PROTECT, related_name="expenses")
    category = models.CharField(max_length=80, blank=True)
    description = models.CharField(max_length=255)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    payment_method = models.ForeignKey(
        PaymentMethod, on_delete=models.PROTECT, null=True, blank=True, related_name="expenses"
    )
    created_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, related_name="+")
    approved_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    paid_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reject_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("status", "branch"))]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_uzs__gt=0), name="expense_amount_positive"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"expense#{self.pk}:{self.status}:{self.amount_uzs}"
