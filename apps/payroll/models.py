"""Payroll register models.

The values that make up a payslip are frozen when a period is run.  Mutable
directory relationships and payout policies are deliberately represented by
write-time snapshots on :class:`PayrollLineItem`; later transfers or policy
changes therefore cannot rewrite historical payroll.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

PRINCIPAL_KIND_CHOICES = (
    ("staff", _("Staff")),
    ("teacher", _("Teacher")),
)


class PayrollPeriod(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PENDING_APPROVAL = "pending_approval", _("Pending approval")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        PAYMENT_IN_PROGRESS = "payment_in_progress", _("Payment in progress")
        PAID = "paid", _("Paid")

    branch = models.ForeignKey("org.Branch", on_delete=models.PROTECT, related_name="payroll_periods")
    department = models.ForeignKey(
        "org.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_periods",
    )
    label = models.CharField(max_length=120)
    period_start = models.DateField()
    period_end = models.DateField()
    pay_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3, default="UZS")
    organization_timezone = models.CharField(max_length=64)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    # A rejected run is never deleted or unfrozen.  Its replacement names the
    # rejected evidence and explains why another run covers the same window.
    correction_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="corrections",
    )
    correction_reason = models.CharField(max_length=255, blank=True)

    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES)
    created_principal_id = models.PositiveBigIntegerField()
    run_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    run_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES, blank=True)
    run_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES, blank=True)
    approved_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    rejected_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES, blank=True)
    rejected_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)

    # Only hashes are retained.  Raw idempotency keys are capability-like and
    # must not enter database dumps or audit payloads.
    run_idempotency_key_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    run_fingerprint = models.CharField(max_length=64, blank=True)
    version = models.PositiveIntegerField(default=1)

    line_count = models.PositiveIntegerField(default=0)
    base_total_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    bonus_total_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    deduction_total_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_total_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    paid_total_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    frozen_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-period_start", "-id")
        indexes = [
            models.Index(
                fields=("branch", "department", "period_start", "period_end", "status"),
                name="payroll_scope_period_idx",
            ),
            models.Index(fields=("status", "pay_date", "id"), name="payroll_status_paydate_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(period_end__gte=F("period_start")), name="pay_period_dates_ok"
            ),
            models.CheckConstraint(condition=Q(currency="UZS"), name="pay_period_currency_uzs"),
            models.CheckConstraint(
                condition=Q(pay_date__isnull=True) | Q(pay_date__gte=F("period_end")),
                name="pay_period_paydate_ok",
            ),
            models.CheckConstraint(condition=Q(line_count__gte=0), name="pay_period_line_count_ok"),
            models.CheckConstraint(
                condition=(
                    Q(base_total_uzs__gte=0)
                    & Q(bonus_total_uzs__gte=0)
                    & Q(deduction_total_uzs__gte=0)
                    & Q(net_total_uzs__gte=0)
                    & Q(paid_total_uzs__gte=0)
                    & Q(paid_total_uzs__lte=F("net_total_uzs"))
                ),
                name="pay_period_totals_ok",
            ),
            models.CheckConstraint(
                condition=(
                    Q(correction_of__isnull=True, correction_reason="")
                    | Q(correction_of__isnull=False) & ~Q(correction_reason="")
                ),
                name="pay_period_correction_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(run_principal_kind="", run_principal_id__isnull=True)
                    | (~Q(run_principal_kind="") & Q(run_principal_id__isnull=False))
                ),
                name="pay_period_run_actor_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(approved_principal_kind="", approved_principal_id__isnull=True)
                    | (~Q(approved_principal_kind="") & Q(approved_principal_id__isnull=False))
                ),
                name="pay_period_approve_actor_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(rejected_principal_kind="", rejected_principal_id__isnull=True)
                    | (~Q(rejected_principal_kind="") & Q(rejected_principal_id__isnull=False))
                ),
                name="pay_period_reject_actor_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(run_idempotency_key_hash__isnull=True, run_fingerprint="")
                    | Q(run_idempotency_key_hash__isnull=False) & ~Q(run_fingerprint="")
                ),
                name="pay_period_run_key_shape",
            ),
            models.UniqueConstraint(
                fields=("branch", "period_start", "period_end"),
                condition=Q(department__isnull=True, correction_of__isnull=True),
                name="pay_period_unique_branch_window",
            ),
            models.UniqueConstraint(
                fields=("branch", "department", "period_start", "period_end"),
                condition=Q(department__isnull=False, correction_of__isnull=True),
                name="pay_period_unique_dept_window",
            ),
            models.UniqueConstraint(
                fields=("correction_of",),
                condition=Q(correction_of__isnull=False),
                name="pay_period_one_correction",
            ),
        ]


class PayrollLineItem(models.Model):
    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="line_items")
    teacher = models.ForeignKey(
        "teachers.TeacherProfile", on_delete=models.PROTECT, related_name="payroll_line_items"
    )
    branch_at_run = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, related_name="payroll_line_items"
    )
    department_at_run = models.ForeignKey(
        "org.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_line_items",
    )
    teacher_user_id_snapshot = models.PositiveBigIntegerField()
    teacher_name_snapshot = models.CharField(max_length=255)
    teacher_code_snapshot = models.CharField(max_length=150)
    payout_policy_id_snapshot = models.PositiveBigIntegerField()
    payout_method_snapshot = models.CharField(max_length=32)
    payout_policy_snapshot = models.JSONField(default=dict)
    calculation_breakdown = models.JSONField(default=dict)
    currency = models.CharField(max_length=3)
    base_amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    bonus_amount_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    deduction_amount_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("teacher_name_snapshot", "id")
        indexes = [
            models.Index(fields=("period", "teacher", "id"), name="pay_line_period_teacher_idx"),
            models.Index(
                fields=("branch_at_run", "department_at_run", "created_at", "id"),
                name="pay_line_scope_time_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(fields=("period", "teacher"), name="pay_line_unique_teacher"),
            models.CheckConstraint(condition=Q(currency="UZS"), name="pay_line_currency_uzs"),
            models.CheckConstraint(
                condition=(
                    Q(base_amount_uzs__gte=0)
                    & Q(bonus_amount_uzs__gte=0)
                    & Q(deduction_amount_uzs__gte=0)
                    & Q(net_amount_uzs__gte=0)
                ),
                name="pay_line_amounts_positive",
            ),
            models.CheckConstraint(
                condition=Q(
                    net_amount_uzs=F("base_amount_uzs") + F("bonus_amount_uzs") - F("deduction_amount_uzs")
                ),
                name="pay_line_net_matches",
            ),
        ]


class PayrollPayslip(models.Model):
    line_item = models.OneToOneField(PayrollLineItem, on_delete=models.PROTECT, related_name="payslip")
    document_number = models.CharField(max_length=64, unique=True)
    snapshot = models.JSONField(default=dict)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-generated_at", "-id")


class PayrollAdjustment(models.Model):
    class Kind(models.TextChoices):
        BONUS = "bonus", _("Bonus")
        DEDUCTION = "deduction", _("Deduction")

    class State(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        APPLIED = "applied", _("Applied")

    teacher = models.ForeignKey(
        "teachers.TeacherProfile", on_delete=models.PROTECT, related_name="payroll_adjustments"
    )
    branch = models.ForeignKey("org.Branch", on_delete=models.PROTECT, related_name="payroll_adjustments")
    department = models.ForeignKey(
        "org.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_adjustments",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, default="UZS")
    effective_period_start = models.DateField()
    effective_period_end = models.DateField()
    reason = models.CharField(max_length=255)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING, db_index=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES)
    created_principal_id = models.PositiveBigIntegerField()
    decided_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES, blank=True)
    decided_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=255, blank=True)
    applied_line = models.ForeignKey(
        PayrollLineItem,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustments",
    )
    idempotency_key_hash = models.CharField(max_length=64, unique=True)
    operation_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=(
                    "branch",
                    "department",
                    "effective_period_start",
                    "effective_period_end",
                    "state",
                ),
                name="pay_adjust_scope_period_idx",
            ),
            models.Index(fields=("teacher", "state", "created_at"), name="pay_adjust_teacher_state_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount_uzs__gt=0), name="pay_adjust_amount_positive"),
            models.CheckConstraint(condition=Q(currency="UZS"), name="pay_adjust_currency_uzs"),
            models.CheckConstraint(
                condition=Q(effective_period_end__gte=F("effective_period_start")),
                name="pay_adjust_dates_ok",
            ),
            models.CheckConstraint(
                condition=(
                    Q(decided_principal_kind="", decided_principal_id__isnull=True, decided_at__isnull=True)
                    | (
                        ~Q(decided_principal_kind="")
                        & Q(decided_principal_id__isnull=False)
                        & Q(decided_at__isnull=False)
                    )
                ),
                name="pay_adjust_decider_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(state="applied", applied_line__isnull=False)
                    | ~Q(state="applied") & Q(applied_line__isnull=True)
                ),
                name="pay_adjust_applied_shape",
            ),
        ]


class PayrollPeriodEvent(models.Model):
    class Action(models.TextChoices):
        RUN = "run", _("Run")
        APPROVE = "approve", _("Approve")
        REJECT = "reject", _("Reject")
        PAYMENT = "payment", _("Payment")
        REVERSAL = "reversal", _("Reversal")

    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="events")
    action = models.CharField(max_length=16, choices=Action.choices)
    actor = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    actor_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES)
    actor_principal_id = models.PositiveBigIntegerField()
    note = models.CharField(max_length=255, blank=True)
    idempotency_key_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    operation_fingerprint = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=("period", "created_at", "id"), name="pay_event_period_time_idx")]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(idempotency_key_hash__isnull=True, operation_fingerprint="")
                    | Q(idempotency_key_hash__isnull=False) & ~Q(operation_fingerprint="")
                ),
                name="pay_event_key_shape",
            )
        ]


class PayrollAdjustmentEvent(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", _("Created")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        APPLIED = "applied", _("Applied")
        RELEASED = "released", _("Released")

    adjustment = models.ForeignKey(PayrollAdjustment, on_delete=models.PROTECT, related_name="events")
    action = models.CharField(max_length=16, choices=Action.choices)
    actor = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    actor_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES)
    actor_principal_id = models.PositiveBigIntegerField()
    note = models.CharField(max_length=255, blank=True)
    idempotency_key_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    operation_fingerprint = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=("adjustment", "created_at", "id"), name="pay_adj_event_time_idx")]


class PayrollReconciliation(models.Model):
    class Kind(models.TextChoices):
        PAYMENT = "payment", _("Payment")
        REVERSAL = "reversal", _("Reversal")

    line_item = models.ForeignKey(PayrollLineItem, on_delete=models.PROTECT, related_name="reconciliations")
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.PAYMENT)
    reverses = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversal",
    )
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    payment_method = models.ForeignKey(
        "finance.PaymentMethod", on_delete=models.PROTECT, related_name="payroll_reconciliations"
    )
    external_reference = models.CharField(max_length=128)
    paid_at = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    ledger_entry = models.OneToOneField(
        "approvals.LedgerEntry", on_delete=models.PROTECT, related_name="payroll_reconciliation"
    )
    recorded_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    recorded_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES)
    recorded_principal_id = models.PositiveBigIntegerField()
    idempotency_key_hash = models.CharField(max_length=64, unique=True)
    operation_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=("line_item", "kind", "created_at"), name="pay_recon_line_kind_idx"),
            models.Index(fields=("paid_at", "id"), name="pay_recon_paid_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("payment_method", "external_reference"),
                name="pay_recon_external_unique",
            ),
            models.CheckConstraint(condition=Q(amount_uzs__gt=0), name="pay_recon_amount_positive"),
            models.CheckConstraint(condition=Q(currency="UZS"), name="pay_recon_currency_uzs"),
            models.CheckConstraint(
                condition=(
                    Q(kind="payment", reverses__isnull=True)
                    | Q(kind="reversal", reverses__isnull=False) & ~Q(reason="")
                ),
                name="pay_recon_reversal_shape",
            ),
        ]


class PayrollExport(models.Model):
    class Format(models.TextChoices):
        XLSX = "xlsx", _("Excel")
        PDF = "pdf", _("PDF")

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        RUNNING = "running", _("Running")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="exports")
    format = models.CharField(max_length=8, choices=Format.choices)
    filters = models.JSONField(default=dict)
    requested_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    requested_principal_kind = models.CharField(max_length=16, choices=PRINCIPAL_KIND_CHOICES)
    requested_principal_id = models.PositiveBigIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    idempotency_key_hash = models.CharField(max_length=64, unique=True)
    operation_fingerprint = models.CharField(max_length=64)
    s3_key = models.CharField(max_length=512, blank=True)
    file_bytes = models.PositiveBigIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("period", "status", "created_at"), name="pay_export_period_status_idx"),
            models.Index(fields=("status", "created_at"), name="pay_export_status_time_idx"),
        ]
