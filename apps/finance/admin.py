from django.contrib import admin

from apps.finance.models import (
    CashierShift,
    Discount,
    FeeSchedule,
    Invoice,
    InvoiceLine,
    PaymentAllocation,
    PaymentPlan,
    PaymentPlanInstallment,
    Refund,
)


@admin.register(FeeSchedule)
class FeeScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "cohort", "amount_uzs", "billing_period", "is_active")
    list_filter = ("is_active", "billing_period")
    search_fields = ("name",)
    autocomplete_fields = ("cohort",)
    list_select_related = ("cohort",)


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "student", "status", "total_uzs", "due_date", "issue_date")
    list_filter = ("status", "currency")
    search_fields = ("number",)
    date_hierarchy = "issue_date"
    inlines = (InvoiceLineInline, PaymentAllocationInline)
    readonly_fields = ("number", "fx_rate_usd", "fx_source", "total_usd")
    autocomplete_fields = ("student", "cohort", "fee_schedule", "created_by")
    list_select_related = ("student",)


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = ("student", "discount_type", "percent", "fixed_amount_uzs", "is_active")
    list_filter = ("discount_type", "is_active")
    autocomplete_fields = ("student", "approved_by")
    list_select_related = ("student",)


class InstallmentInline(admin.TabularInline):
    model = PaymentPlanInstallment
    extra = 0


@admin.register(PaymentPlan)
class PaymentPlanAdmin(admin.ModelAdmin):
    list_display = ("id", "invoice", "created_at")
    inlines = (InstallmentInline,)
    autocomplete_fields = ("invoice", "created_by")
    list_select_related = ("invoice",)


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("id", "invoice", "amount_uzs", "state", "payment_id", "created_at")
    list_filter = ("state",)
    autocomplete_fields = ("invoice", "requested_by", "approved_by")
    list_select_related = ("invoice",)


@admin.register(CashierShift)
class CashierShiftAdmin(admin.ModelAdmin):
    list_display = ("id", "cashier", "branch", "status", "opened_at", "closed_at", "discrepancy_uzs")
    list_filter = ("status",)
    autocomplete_fields = ("cashier", "branch")
    list_select_related = ("cashier", "branch")
