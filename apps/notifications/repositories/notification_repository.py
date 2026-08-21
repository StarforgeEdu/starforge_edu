"""ORM-backed notifications repositories (own-rows feed + preferences; template CRUD)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.notifications import selectors
from apps.notifications.interfaces.repositories import (
    INotificationRepository,
    INotificationTemplateRepository,
)
from apps.notifications.models import (
    DELIVERABLE_ATTRIBUTION_STATUSES,
    Notification,
    NotificationPreference,
    NotificationTemplate,
)
from core.repositories import BaseRepository


class NotificationRepository(BaseRepository[Notification], INotificationRepository):
    model = Notification

    def feed(
        self, *, user, recipient_principal_kind: str, recipient_principal_id: int
    ) -> QuerySet[Notification]:
        # feed_for_user orders (-created_at); add the id tiebreaker for keyset cursor
        # pagination (deterministic, no skipped/duplicated rows on same-ms ties).
        return selectors.feed_for_user(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
        ).order_by("-created_at", "-id")

    def get_own(
        self,
        *,
        user,
        recipient_principal_kind: str,
        recipient_principal_id: int,
        pk: int,
    ) -> Notification | None:
        return Notification.objects.filter(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
            attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES,
            pk=pk,
        ).first()

    def unread_count(self, *, user, recipient_principal_kind: str, recipient_principal_id: int) -> int:
        return selectors.unread_count(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
        )

    def preferences(
        self, *, user, recipient_principal_kind: str, recipient_principal_id: int
    ) -> QuerySet[NotificationPreference]:
        return selectors.preferences_for_user(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
        )


class NotificationTemplateRepository(BaseRepository[NotificationTemplate], INotificationTemplateRepository):
    model = NotificationTemplate

    def queryset(self) -> QuerySet[NotificationTemplate]:
        return NotificationTemplate.objects.all()

    def get(self, *, pk: int) -> NotificationTemplate | None:
        return NotificationTemplate.objects.filter(pk=pk).first()

    def add(self, *, data: dict) -> NotificationTemplate:
        return NotificationTemplate.objects.create(**data)

    def apply_changes(
        self, template: NotificationTemplate, *, changes: dict[str, Any]
    ) -> NotificationTemplate:
        for field, value in changes.items():
            setattr(template, field, value)
        if changes:
            template.save(update_fields=[*changes.keys(), "updated_at"])
        return template

    def delete(self, template: NotificationTemplate) -> None:
        template.delete()
