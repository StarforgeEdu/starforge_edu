"""WebSocket routes owned by the messaging domain."""

from __future__ import annotations

from django.urls import path

from apps.messaging.consumers import ThreadConsumer

websocket_urlpatterns = [
    path("ws/messaging/threads/<int:thread_id>/", ThreadConsumer.as_asgi()),
]
