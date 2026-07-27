"""Tenant-side payments URLConf (included at /api/v1/payments/)."""

from django.urls import path

from apps.payments.views.v1 import payment_views as views

urlpatterns = [
    # Provider credential configs
    path("provider-configs/", views.provider_configs_collection_view, name="provider-configs-list"),
    path("provider-configs/<int:pk>/", views.provider_config_detail_view, name="provider-configs-detail"),
    # Payment actions (collection-level actions precede the <pk> detail)
    path("checkout/", views.payment_checkout_view, name="payment-checkout"),
    path("cash/", views.payment_cash_view, name="payment-cash"),
    path("reconciliation/", views.payment_reconciliation_view, name="payment-reconciliation"),
    path("<int:pk>/allocate/", views.payment_allocate_view, name="payment-allocate"),
    path("<int:pk>/refund/", views.payment_refund_view, name="payment-refund"),
    path("<int:pk>/receipt/", views.payment_receipt_view, name="payment-receipt"),
    # Payment log
    path("", views.payments_collection_view, name="payment-list"),
    path("<int:pk>/", views.payment_detail_view, name="payment-detail"),
]
