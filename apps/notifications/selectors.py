"""Notifications read-side selectors (D3-C-9).

Feed + preference reads are scoped to the requesting user — a user only ever
sees their OWN notification rows (enforced here, not the gate; mirrors the TD-5
read-scoping pattern in the academics/attendance selectors).
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.notifications.models import Notification, NotificationPreference


def feed_for_user(*, user) -> QuerySet[Notification]:
    """The user's own notifications, newest first (cursor-paginated by the view)."""
    # user is surfaced (id + user_name) in notification_to_dict, so join it here —
    # no extra query per row (keeps the feed query budget flat).
    return Notification.objects.select_related("user").filter(user=user).order_by("-created_at")


def unread_count(*, user) -> int:
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


def preferences_for_user(*, user) -> QuerySet[NotificationPreference]:
    return NotificationPreference.objects.filter(user=user).order_by("event_type", "channel")
