"""Parent-domain routes — plain function views (off DRF). Mounted at /api/v1/parents/.

Specific prefixes (me/children, guardians, pickups) are listed before the parent
catch-all. The parent detail/action routes use ``<int:pk>`` so they never capture
the literal "me"/"guardians"/"pickups" segments.
"""

from __future__ import annotations

from django.urls import path

from apps.parents.views.v1.guardian_views import guardian_detail_view, guardians_collection_view
from apps.parents.views.v1.parent_views import (
    parent_child_report_view,
    parent_children_view,
    parent_credentials_view,
    parent_detail_view,
    parent_students_view,
    parents_collection_view,
)
from apps.parents.views.v1.pickup_views import pickup_detail_view, pickups_collection_view

urlpatterns = [
    # Parent self-service (F2-6) — before the catch-all parent routes.
    path("me/children/", parent_children_view, name="parent-my-children"),
    path("me/children/<int:student_id>/report/", parent_child_report_view, name="parent-child-report"),
    # Guardians (parent↔student links).
    path("guardians/", guardians_collection_view, name="guardians-collection"),
    path("guardians/<int:pk>/", guardian_detail_view, name="guardians-detail"),
    # Pickup authorizations.
    path("pickups/", pickups_collection_view, name="pickups-collection"),
    path("pickups/<int:pk>/", pickup_detail_view, name="pickups-detail"),
    # Parents.
    path("", parents_collection_view, name="parents-collection"),
    path("<int:pk>/", parent_detail_view, name="parents-detail"),
    path("<int:pk>/students/", parent_students_view, name="parents-students"),
    path("<int:pk>/credentials/", parent_credentials_view, name="parents-credentials"),
]
