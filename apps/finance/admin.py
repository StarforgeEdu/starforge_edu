from django.contrib import admin

from apps.finance.models import (
    CashierShift,
    Discount,
    Expense,
    FeeSchedule,
    Invoice,
    InvoiceLine,
    PaymentAllocation,
    PaymentPlan,
    PaymentPlanInstallment,
    Refund,
)
from core.admin_mixins import ReadOnlyAdmin


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(FeeSchedule)
class FeeScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "cohort", "amount_uzs", "billing_period", "is_active")
    list_filter = ("is_active", "billing_period")
    search_fields = ("name",)
    autocomplete_fields = ("cohort",)
    list_select_related = ("cohort",)


class InvoiceLineInline(ReadOnlyInline):
    model = InvoiceLine
    fields = ("description", "line_type", "quantity", "unit_price_uzs", "amount_uzs", "created_at")
    readonly_fields = fields


class PaymentAllocationInline(ReadOnlyInline):
    model = PaymentAllocation
    fields = ("payment_id", "amount_uzs", "created_at")
    readonly_fields = fields


@admin.register(Invoice)
class InvoiceAdmin(ReadOnlyAdmin):
    list_display = ("number", "student", "status", "total_uzs", "due_date", "issue_date")
    list_filter = ("status", "currency")
    search_fields = ("number",)
    date_hierarchy = "issue_date"
    inlines = (InvoiceLineInline, PaymentAllocationInline)
    readonly_fields = ("number", "fx_rate_usd", "fx_source", "total_usd")
    autocomplete_fields = ("student", "cohort", "fee_schedule", "created_by")
    list_select_related = ("student",)


@admin.register(Discount)
class DiscountAdmin(ReadOnlyAdmin):
    list_display = ("student", "discount_type", "percent", "fixed_amount_uzs", "is_active")
    list_filter = ("discount_type", "is_active")
    autocomplete_fields = ("student", "approved_by")
    list_select_related = ("student",)


class InstallmentInline(ReadOnlyInline):
    model = PaymentPlanInstallment
    fields = ("due_date", "amount_uzs", "status", "created_at")
    readonly_fields = fields


@admin.register(PaymentPlan)
class PaymentPlanAdmin(ReadOnlyAdmin):
    list_display = ("id", "invoice", "created_at")
    inlines = (InstallmentInline,)
    autocomplete_fields = ("invoice", "created_by")
    list_select_related = ("invoice",)


@admin.register(Refund)
class RefundAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "invoice",
        "amount_uzs",
        "state",
        "provider",
        "provider_refund_id",
        "ledger_entry",
        "payment_id",
        "created_at",
    )
    list_filter = ("state",)
    autocomplete_fields = ("invoice", "requested_by", "approved_by")
    list_select_related = ("invoice",)


@admin.register(CashierShift)
class CashierShiftAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "cashier",
        "branch",
        "status",
        "opened_at",
        "closed_at",
        "closed_by",
        "discrepancy_uzs",
    )
    list_filter = ("status",)
    autocomplete_fields = ("cashier", "branch")
    list_select_related = ("cashier", "branch")


@admin.register(Expense)
class ExpenseAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "description",
        "branch",
        "amount_uzs",
        "status",
        "created_by",
        "approved_by",
        "paid_by",
    )
    list_filter = ("status", "branch", "category")
    search_fields = ("description", "category")
    list_select_related = (
        "branch",
        "created_by",
        "approved_by",
        "paid_by",
        "approval_request",
    )


@admin.register(InvoiceLine)
class InvoiceLineAdmin(ReadOnlyAdmin):
    list_display = ("id", "invoice", "line_type", "description", "amount_uzs", "created_at")
    list_filter = ("line_type",)
    search_fields = ("invoice__number", "description")
    list_select_related = ("invoice",)


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(ReadOnlyAdmin):
    list_display = ("id", "invoice", "payment_id", "amount_uzs", "created_at")
    search_fields = ("invoice__number", "payment_id")
    list_select_related = ("invoice",)


@admin.register(PaymentPlanInstallment)
class PaymentPlanInstallmentAdmin(ReadOnlyAdmin):
    list_display = ("id", "plan", "due_date", "amount_uzs", "status", "created_at")
    list_filter = ("status",)
    list_select_related = ("plan",)
