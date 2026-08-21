from django.urls import path

from apps.procurement.views.v1.purchase_order_views import (
    purchase_order_detail_view,
    purchase_orders_collection_view,
)

urlpatterns = [
    path("", purchase_orders_collection_view, name="procurement-list"),
    path("<int:pk>/", purchase_order_detail_view, name="procurement-detail"),
]
