from django.urls import path

from apps.notifications.views.v1.notification_views import (
    announcement_view,
    notification_read_all_view,
    notification_read_view,
    notification_unread_count_view,
    notifications_collection_view,
    preferences_view,
    template_detail_view,
    templates_collection_view,
)

urlpatterns = [
    # Explicit routes must precede the bare `{pk}`/feed patterns.
    path("templates/", templates_collection_view, name="notification-template-list"),
    path("templates/<int:pk>/", template_detail_view, name="notification-template-detail"),
    path("preferences/", preferences_view, name="notification-preferences"),
    path("announcements/", announcement_view, name="notification-announcements"),
    path("unread-count/", notification_unread_count_view, name="notification-unread-count"),
    path("read-all/", notification_read_all_view, name="notification-read-all"),
    path("<int:pk>/read/", notification_read_view, name="notification-read"),
    path("", notifications_collection_view, name="notification-list"),
]
