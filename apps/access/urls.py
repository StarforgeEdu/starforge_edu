"""Access-config routes (A-2) — plain function views (off DRF). Mounted at /api/v1/access/."""

from __future__ import annotations

from django.urls import path

from apps.access.views.v1.access_views import (
    access_permissions_view,
    access_roles_view,
    override_detail_view,
    overrides_collection_view,
)

urlpatterns = [
    path("roles/", access_roles_view, name="access-roles"),
    path("permissions/", access_permissions_view, name="access-permissions"),
    path("overrides/", overrides_collection_view, name="access-overrides-collection"),
    path("overrides/<int:pk>/", override_detail_view, name="access-overrides-detail"),
]
