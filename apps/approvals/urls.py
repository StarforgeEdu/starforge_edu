from django.urls import path

from apps.approvals.views.v1 import approvals_views as views

urlpatterns = [
    # Approval requests (+ decision/disburse actions before the <pk> detail)
    path("requests/", views.approval_requests_collection_view, name="approval-request-list"),
    path("requests/<int:pk>/approve/", views.approval_request_approve_view, name="approval-request-approve"),
    path("requests/<int:pk>/reject/", views.approval_request_reject_view, name="approval-request-reject"),
    path("requests/<int:pk>/cancel/", views.approval_request_cancel_view, name="approval-request-cancel"),
    path(
        "requests/<int:pk>/disburse/", views.approval_request_disburse_view, name="approval-request-disburse"
    ),
    path("requests/<int:pk>/", views.approval_request_detail_view, name="approval-request-detail"),
    # Ledger (read-only)
    path("ledger/", views.ledger_collection_view, name="ledger-entry-list"),
    path("ledger/<int:pk>/", views.ledger_detail_view, name="ledger-entry-detail"),
]
