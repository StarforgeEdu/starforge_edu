"""Notifications response presenters (the DRF serializer output shapes)."""

from __future__ import annotations

from apps.notifications.models import Notification, NotificationPreference, NotificationTemplate


def notification_to_dict(n: Notification) -> dict:
    return {
        "id": n.id,
        # Recipient id + a readable companion, resolved from the feed selector's
        # select_related("user") — a client renders "for <name>" without a second call.
        "user": n.user_id,
        "user_name": n.user.get_full_name(),
        "event_type": n.event_type,
        "title": n.title,
        "body": n.body,
        "data": n.data,
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "created_at": n.created_at.isoformat(),
    }


def preference_to_dict(p: NotificationPreference) -> dict:
    return {"event_type": p.event_type, "channel": p.channel, "enabled": p.enabled}


def template_to_dict(t: NotificationTemplate) -> dict:
    return {
        "id": t.id,
        "event_type": t.event_type,
        "channel": t.channel,
        "locale": t.locale,
        "subject": t.subject,
        "body": t.body,
        "is_active": t.is_active,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }
