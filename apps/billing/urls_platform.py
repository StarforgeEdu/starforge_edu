"""Control-center subscription URLs (D4-LE-3) — PUBLIC schema, staff-only.

Mounted in config/urls_public.py under `api/v1/platform/` (a flat
`/platform/subscriptions/` surface for the control center, distinct from the
legacy `/platform/billing/subscriptions/{center_id}/` lookup-by-center view).
Plain function views (off DRF); lookup is by SUBSCRIPTION id.
"""

from __future__ import annotations

from django.urls import path

from apps.billing.views.v1 import billing_views as views

urlpatterns = [
    path("subscriptions/", views.platform_subscriptions_collection_view, name="platform-subscriptions"),
    path(
        "subscriptions/<int:pk>/",
        views.platform_subscription_detail_view,
        name="platform-subscription-detail",
    ),
]
