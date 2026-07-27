from django.urls import path

from apps.messaging.views.v1.thread_views import (
    thread_detail_view,
    thread_messages_view,
    thread_read_view,
    threads_collection_view,
)

urlpatterns = [
    path("threads/", threads_collection_view, name="thread-list"),
    path("threads/<int:pk>/", thread_detail_view, name="thread-detail"),
    path("threads/<int:pk>/messages/", thread_messages_view, name="thread-messages"),
    path("threads/<int:pk>/read/", thread_read_view, name="thread-read"),
]
