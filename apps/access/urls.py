"""Access-config routes (A-2) — plain function views (off DRF). Mounted at /api/v1/access/."""

from __future__ import annotations

from django.urls import path

from apps.access.views.v1.access_views import (
    access_permissions_view,
    access_roles_view,
    account_type_assignment_detail_view,
    account_type_assignments_view,
    account_type_detail_view,
    account_type_effective_permissions_view,
    account_type_permissions_view,
    account_types_collection_view,
    override_detail_view,
    overrides_collection_view,
)

urlpatterns = [
    path("types/", account_types_collection_view, name="access-account-types"),
    path(
        "types/effective-permissions/",
        account_type_effective_permissions_view,
        name="access-account-type-effective-permissions",
    ),
    path(
        "types/assignments/",
        account_type_assignments_view,
        name="access-account-type-assignments",
    ),
    path(
        "types/assignments/<int:pk>/",
        account_type_assignment_detail_view,
        name="access-account-type-assignment-detail",
    ),
    path("types/<int:pk>/", account_type_detail_view, name="access-account-type-detail"),
    path(
        "types/<int:pk>/permissions/",
        account_type_permissions_view,
        name="access-account-type-permissions",
    ),
    path(
        "types/<int:account_type_pk>/assignments/",
        account_type_assignments_view,
        name="access-account-type-scoped-assignments",
    ),
    path("roles/", access_roles_view, name="access-roles"),
    path("permissions/", access_permissions_view, name="access-permissions"),
    path("overrides/", overrides_collection_view, name="access-overrides-collection"),
    path("overrides/<int:pk>/", override_detail_view, name="access-overrides-detail"),
]
