"""WebSocket routes for the attendance app (D4-LC-4)."""

from __future__ import annotations

from django.urls import path

from apps.attendance.consumers import AttendanceConsumer

websocket_urlpatterns = [
    path("ws/cohorts/<int:cohort_id>/attendance/", AttendanceConsumer.as_asgi()),
]
