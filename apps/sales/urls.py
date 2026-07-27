from django.urls import path

from apps.sales.views.v1.sale_views import (
    sale_detail_view,
    sale_refund_view,
    sales_collection_view,
)

urlpatterns = [
    path("", sales_collection_view, name="sale-list"),
    path("<int:pk>/", sale_detail_view, name="sale-detail"),
    path("<int:pk>/refund/", sale_refund_view, name="sale-refund"),
]
