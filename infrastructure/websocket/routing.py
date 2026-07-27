"""Top-level Channels URL routing (D4-LC-5).

Concatenates each app's ``websocket_urlpatterns`` and keeps the v1 ``ws/ping/``
smoke route. ``config/asgi.py`` imports ``websocket_urlpatterns`` from here — do
not change that import path.

Routes:
  - ws/ping/                              -> PingConsumer (smoke, unchanged)
  - ws/notifications/                     -> NotificationConsumer (apps.notifications)
  - ws/cohorts/<cohort_id>/attendance/    -> AttendanceConsumer (apps.attendance)
"""

from __future__ import annotations

from django.urls import path

from apps.attendance.routing import websocket_urlpatterns as attendance_ws_urlpatterns
from apps.notifications.routing import websocket_urlpatterns as notifications_ws_urlpatterns

from .consumers import PingConsumer

websocket_urlpatterns = [
    path("ws/ping/", PingConsumer.as_asgi()),
    *notifications_ws_urlpatterns,
    *attendance_ws_urlpatterns,
]
