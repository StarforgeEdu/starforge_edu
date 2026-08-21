"""Billing platform URLs — included in config/urls_public.py under
`api/v1/platform/billing/` (PUBLIC schema only). Plain function views (off DRF).
"""

from __future__ import annotations

from django.urls import path

from apps.billing.views.v1 import billing_views as views

urlpatterns = [
    path("usage/", views.usage_view, name="billing-usage"),
    path("ai-charges/", views.ai_charges_view, name="billing-ai-charges"),
    path("checkout/", views.checkout_view, name="billing-checkout"),
    path("plans/", views.plans_collection_view, name="billing-plans"),
    path("plans/<int:pk>/", views.plan_detail_view, name="billing-plan-detail"),
    # Subscriptions here are looked up by CENTER id (a Center has one subscription).
    path(
        "subscriptions/<int:center_id>/",
        views.subscription_by_center_view,
        name="billing-subscription-by-center",
    ),
]
