from django.urls import path

from apps.loans.views.v1.loan_views import (
    loan_detail_view,
    loan_repay_view,
    loan_repayments_view,
    loans_collection_view,
)

urlpatterns = [
    path("", loans_collection_view, name="loan-list"),
    path("<int:pk>/", loan_detail_view, name="loan-detail"),
    path("<int:pk>/repay/", loan_repay_view, name="loan-repay"),
    path("<int:pk>/repayments/", loan_repayments_view, name="loan-repayments"),
]
