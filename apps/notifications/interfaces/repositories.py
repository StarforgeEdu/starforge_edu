"""Notifications-domain repository ports.

The feed + preferences are OWN-ROWS-ONLY (scoped to the requesting user), so a detail
mutation (mark one read) resolves against the own-rows queryset -> 404 on someone else's
pk. Templates are centre-wide admin config (director/IT only, gated at the view).
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.notifications.models import Notification, NotificationPreference, NotificationTemplate
from core.interfaces import IBaseRepository


class INotificationRepository(IBaseRepository[Notification]):
    def feed(self, *, user) -> QuerySet[Notification]:
        raise NotImplementedError

    def get_own(self, *, user, pk: int) -> Notification | None:
        raise NotImplementedError

    def unread_count(self, *, user) -> int:
        raise NotImplementedError

    def preferences(self, *, user) -> QuerySet[NotificationPreference]:
        raise NotImplementedError


class INotificationTemplateRepository(IBaseRepository[NotificationTemplate]):
    def queryset(self) -> QuerySet[NotificationTemplate]:
        raise NotImplementedError

    def get(self, *, pk: int) -> NotificationTemplate | None:
        raise NotImplementedError

    def add(self, *, data: dict) -> NotificationTemplate:
        raise NotImplementedError

    def apply_changes(self, template: NotificationTemplate, *, changes: dict) -> NotificationTemplate:
        raise NotImplementedError

    def delete(self, template: NotificationTemplate) -> None:
        raise NotImplementedError
