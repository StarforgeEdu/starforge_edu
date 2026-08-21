"""Cover routes — plain function views (off DRF). Mounted at /api/v1/cover/."""

from __future__ import annotations

from django.urls import path

from apps.covers.views.v1.cover_views import (
    cover_assign_view,
    cover_cancel_view,
    cover_claim_view,
    cover_detail_view,
    cover_open_pool_view,
    cover_pool_view,
    cover_reject_view,
    covers_collection_view,
)

urlpatterns = [
    path("", covers_collection_view, name="covers-collection"),
    path("pool/", cover_pool_view, name="covers-pool"),
    path("<int:pk>/", cover_detail_view, name="covers-detail"),
    path("<int:pk>/assign/", cover_assign_view, name="covers-assign"),
    path("<int:pk>/open-pool/", cover_open_pool_view, name="covers-open-pool"),
    path("<int:pk>/claim/", cover_claim_view, name="covers-claim"),
    path("<int:pk>/cancel/", cover_cancel_view, name="covers-cancel"),
    path("<int:pk>/reject/", cover_reject_view, name="covers-reject"),
]
