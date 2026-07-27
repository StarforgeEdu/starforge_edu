"""WebSocket routes for the notifications app (D4-LC-3)."""

from __future__ import annotations

from django.urls import path

from apps.notifications.consumers import NotificationConsumer

websocket_urlpatterns = [
    path("ws/notifications/", NotificationConsumer.as_asgi()),
]
