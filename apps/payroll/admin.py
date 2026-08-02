from django.contrib import admin

from apps.payroll.models import (
    PayrollAdjustment,
    PayrollAdjustmentEvent,
    PayrollExport,
    PayrollLineItem,
    PayrollPayslip,
    PayrollPeriod,
    PayrollPeriodEvent,
    PayrollReconciliation,
)


class ImmutablePayrollAdmin(admin.ModelAdmin):
    """Payroll evidence is observed in admin and mutated only by domain services."""

    list_per_page = 50

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PayrollPeriod)
class PayrollPeriodAdmin(ImmutablePayrollAdmin):
    list_display = ("id", "label", "branch", "department", "period_start", "period_end", "status")
    list_filter = ("status", "branch", "department")


@admin.register(PayrollLineItem)
class PayrollLineItemAdmin(ImmutablePayrollAdmin):
    list_display = ("id", "period", "teacher_name_snapshot", "net_amount_uzs", "currency")


@admin.register(PayrollPayslip)
class PayrollPayslipAdmin(ImmutablePayrollAdmin):
    list_display = ("id", "document_number", "line_item", "generated_at")


@admin.register(PayrollAdjustment)
class PayrollAdjustmentAdmin(ImmutablePayrollAdmin):
    list_display = ("id", "teacher", "kind", "amount_uzs", "state", "effective_period_start")
    list_filter = ("kind", "state", "branch", "department")


@admin.register(PayrollReconciliation)
class PayrollReconciliationAdmin(ImmutablePayrollAdmin):
    list_display = ("id", "line_item", "kind", "amount_uzs", "payment_method", "paid_at")
    list_filter = ("kind", "payment_method")


@admin.register(PayrollExport)
class PayrollExportAdmin(ImmutablePayrollAdmin):
    list_display = ("id", "period", "format", "status", "created_at", "finished_at")
    list_filter = ("format", "status")


admin.site.register(PayrollPeriodEvent, ImmutablePayrollAdmin)
admin.site.register(PayrollAdjustmentEvent, ImmutablePayrollAdmin)
