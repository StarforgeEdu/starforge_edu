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

    def feed(
        self, *, user, recipient_principal_kind: str, recipient_principal_id: int
    ) -> QuerySet[Notification]:
        return self.repository.feed(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
        )

    def get_own(
        self,
        *,
        user,
        recipient_principal_kind: str,
        recipient_principal_id: int,
        pk: int,
    ) -> Notification | None:
        return self.repository.get_own(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
            pk=pk,
        )

    def unread_count(self, *, user, recipient_principal_kind: str, recipient_principal_id: int) -> int:
        return self.repository.unread_count(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
        )

    def mark_read(
        self,
        *,
        user,
        recipient_principal_kind: str,
        recipient_principal_id: int,
        notification_id: int,
    ) -> bool:
        return domain.mark_read(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
            notification_id=notification_id,
        )

    def mark_all_read(self, *, user, recipient_principal_kind: str, recipient_principal_id: int) -> int:
        return domain.mark_all_read(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
        )

    def preferences(
        self, *, user, recipient_principal_kind: str, recipient_principal_id: int
    ) -> QuerySet[NotificationPreference]:
        return self.repository.preferences(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
        )

    def upsert_preferences(
        self,
        *,
        user,
        recipient_principal_kind: str,
        recipient_principal_id: int,
        rows: list[PreferenceRowDTO],
    ) -> list[NotificationPreference]:
        payload = [{"event_type": r.event_type, "channel": r.channel, "enabled": r.enabled} for r in rows]
        return domain.upsert_preferences(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
            rows=payload,
        )

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
