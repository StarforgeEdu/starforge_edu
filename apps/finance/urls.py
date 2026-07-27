"""Finance URLs (mounted at /api/v1/finance/). Plain function views (off DRF).

Action sub-routes + collection actions (cashier-shifts/open/) are declared
before the ``<int:pk>`` detail routes.
"""

from django.urls import path

from apps.finance.views.v1 import finance_views as views

urlpatterns = [
    # fee schedules
    path("fee-schedules/", views.fee_schedules_collection_view, name="finance-fee-schedule-collection"),
    path("fee-schedules/<int:pk>/", views.fee_schedule_detail_view, name="finance-fee-schedule-detail"),
    # invoices
    path("invoices/", views.invoices_collection_view, name="finance-invoice-collection"),
    path("invoices/<int:pk>/void/", views.invoice_void_view, name="finance-invoice-void"),
    path("invoices/<int:pk>/payment-plan/", views.invoice_payment_plan_view, name="finance-invoice-payment-plan"),
    path("invoices/<int:pk>/", views.invoice_detail_view, name="finance-invoice-detail"),
    # discounts
    path("discounts/", views.discounts_collection_view, name="finance-discount-collection"),
    path("discounts/<int:pk>/deactivate/", views.discount_deactivate_view, name="finance-discount-deactivate"),
    path("discounts/<int:pk>/", views.discount_detail_view, name="finance-discount-detail"),
    # payment methods
    path("payment-methods/", views.payment_methods_collection_view, name="finance-payment-method-collection"),
    path("payment-methods/<int:pk>/", views.payment_method_detail_view, name="finance-payment-method-detail"),
    # expenses
    path("expenses/", views.expenses_collection_view, name="finance-expense-collection"),
    path("expenses/<int:pk>/approve/", views.expense_approve_view, name="finance-expense-approve"),
    path("expenses/<int:pk>/reject/", views.expense_reject_view, name="finance-expense-reject"),
    path("expenses/<int:pk>/pay/", views.expense_pay_view, name="finance-expense-pay"),
    path("expenses/<int:pk>/", views.expense_detail_view, name="finance-expense-detail"),
    # cashier shifts
    path("cashier-shifts/", views.cashier_shifts_collection_view, name="finance-cashier-shift-collection"),
    path("cashier-shifts/open/", views.cashier_shift_open_view, name="finance-cashier-shift-open"),
    path("cashier-shifts/<int:pk>/close/", views.cashier_shift_close_view, name="finance-cashier-shift-close"),
    path("cashier-shifts/<int:pk>/report/", views.cashier_shift_report_view, name="finance-cashier-shift-report"),
    path("cashier-shifts/<int:pk>/", views.cashier_shift_detail_view, name="finance-cashier-shift-detail"),
    # standalone endpoints
    path("outstanding/", views.outstanding_balance_view, name="finance-outstanding"),
    path("students/<int:student_id>/statement/", views.statement_request_view, name="finance-statement-request"),
    path("statements/<str:task_id>/", views.statement_result_view, name="finance-statement-result"),
]
