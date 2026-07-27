"""Staff-meeting routes — plain function views (off DRF). Mounted at /api/v1/meetings/."""

from __future__ import annotations

from django.urls import path

from apps.meetings.views.v1.meeting_views import (
    meeting_cancel_view,
    meeting_detail_view,
    meeting_respond_view,
    meetings_collection_view,
    meetings_upcoming_view,
)

urlpatterns = [
    path("", meetings_collection_view, name="meetings-collection"),
    path("upcoming/", meetings_upcoming_view, name="meetings-upcoming"),
    path("<int:pk>/", meeting_detail_view, name="meetings-detail"),
    path("<int:pk>/cancel/", meeting_cancel_view, name="meetings-cancel"),
    path("<int:pk>/respond/", meeting_respond_view, name="meetings-respond"),
]
