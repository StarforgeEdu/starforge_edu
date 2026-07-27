"""Audit routes — plain function views (off DRF). Mounted at /api/v1/audit/.

``export/`` is declared before the ``<int:pk>`` detail route (harmless with an int
converter, but explicit). The trail is read-only: every write verb answers 405.
"""

from __future__ import annotations

from django.urls import path

from apps.audit.views.v1.audit_views import (
    audit_collection_view,
    audit_detail_view,
    audit_export_view,
)

urlpatterns = [
    path("export/", audit_export_view, name="audit-export"),
    path("", audit_collection_view, name="audit-collection"),
    path("<int:pk>/", audit_detail_view, name="audit-detail"),
]
