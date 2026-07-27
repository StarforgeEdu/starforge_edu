"""Notifications services — thin orchestration over the preserved domain fns
(dispatch/mark_read/mark_all_read/upsert_preferences/announce_cohort) + read selectors."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.notifications import services as domain
from apps.notifications.dto.notification_dto import (
    AnnouncementDTO,
    CreateTemplateDTO,
    PreferenceRowDTO,
)
from apps.notifications.interfaces.repositories import (
    INotificationRepository,
    INotificationTemplateRepository,
)
from apps.notifications.interfaces.services import (
    INotificationService,
    INotificationTemplateService,
)
from apps.notifications.models import Notification, NotificationPreference, NotificationTemplate


class NotificationService(INotificationService):
    def __init__(self, repository: INotificationRepository) -> None:
        self.repository = repository

    def feed(self, *, user) -> QuerySet[Notification]:
        return self.repository.feed(user=user)

    def get_own(self, *, user, pk: int) -> Notification | None:
        return self.repository.get_own(user=user, pk=pk)

    def unread_count(self, *, user) -> int:
        return self.repository.unread_count(user=user)

    def mark_read(self, *, user, notification_id: int) -> bool:
        return domain.mark_read(user=user, notification_id=notification_id)

    def mark_all_read(self, *, user) -> int:
        return domain.mark_all_read(user=user)

    def preferences(self, *, user) -> QuerySet[NotificationPreference]:
        return self.repository.preferences(user=user)

    def upsert_preferences(self, *, user, rows: list[PreferenceRowDTO]) -> list[NotificationPreference]:
        payload = [{"event_type": r.event_type, "channel": r.channel, "enabled": r.enabled} for r in rows]
        return domain.upsert_preferences(user=user, rows=payload)

    def announce(self, data: AnnouncementDTO, *, actor) -> dict[str, Any]:
        return domain.announce_cohort(cohort_id=data.cohort_id, title=data.title, body=data.body, actor=actor)


class NotificationTemplateService(INotificationTemplateService):
    def __init__(self, repository: INotificationTemplateRepository) -> None:
        self.repository = repository

    def list(self) -> QuerySet[NotificationTemplate]:
        return self.repository.queryset()

    def get(self, *, pk: int) -> NotificationTemplate | None:
        return self.repository.get(pk=pk)

    def create(self, data: CreateTemplateDTO) -> NotificationTemplate:
        return self.repository.add(
            data={
                "event_type": data.event_type,
                "channel": data.channel,
                "locale": data.locale,
                "subject": data.subject,
                "body": data.body,
                "is_active": data.is_active,
            }
        )

    def update(self, template: NotificationTemplate, changes: dict[str, Any]) -> NotificationTemplate:
        return self.repository.apply_changes(template, changes=changes)

    def delete(self, template: NotificationTemplate) -> None:
        self.repository.delete(template)
