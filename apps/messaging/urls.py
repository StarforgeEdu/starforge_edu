from django.urls import path

from apps.messaging.views.v1.thread_views import (
    attachment_upload_url_view,
    contacts_collection_view,
    message_detail_view,
    message_reaction_detail_view,
    message_reactions_view,
    thread_attachment_download_view,
    thread_detail_view,
    thread_events_view,
    thread_messages_view,
    thread_preferences_view,
    thread_read_view,
    threads_collection_view,
)

urlpatterns = [
    path("attachments/upload-url/", attachment_upload_url_view, name="message-attachment-upload"),
    path("contacts/", contacts_collection_view, name="message-contact-list"),
    path("threads/", threads_collection_view, name="thread-list"),
    path("threads/<int:pk>/", thread_detail_view, name="thread-detail"),
    path("threads/<int:pk>/messages/", thread_messages_view, name="thread-messages"),
    path("messages/<int:pk>/", message_detail_view, name="message-detail"),
    path("messages/<int:pk>/reactions/", message_reactions_view, name="message-reactions"),
    path(
        "messages/<int:pk>/reactions/<str:emoji>/",
        message_reaction_detail_view,
        name="message-reaction-detail",
    ),
    path("threads/<int:pk>/events/", thread_events_view, name="thread-events"),
    path("threads/<int:pk>/read/", thread_read_view, name="thread-read"),
    path(
        "threads/<int:pk>/preferences/",
        thread_preferences_view,
        name="thread-preferences",
    ),
    path(
        "threads/<int:pk>/attachments/download/",
        thread_attachment_download_view,
        name="thread-attachment-download",
    ),
]
