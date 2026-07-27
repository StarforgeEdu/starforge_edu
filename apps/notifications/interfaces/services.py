"""Notifications-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.notifications.dto.notification_dto import (
    AnnouncementDTO,
    CreateTemplateDTO,
    PreferenceRowDTO,
)
from apps.notifications.models import Notification, NotificationPreference, NotificationTemplate


class INotificationService(ABC):
    @abstractmethod
    def feed(self, *, user) -> QuerySet[Notification]: ...

    @abstractmethod
    def get_own(self, *, user, pk: int) -> Notification | None: ...

    @abstractmethod
    def unread_count(self, *, user) -> int: ...

    @abstractmethod
    def mark_read(self, *, user, notification_id: int) -> bool: ...

    @abstractmethod
    def mark_all_read(self, *, user) -> int: ...

    @abstractmethod
    def preferences(self, *, user) -> QuerySet[NotificationPreference]: ...

    @abstractmethod
    def upsert_preferences(self, *, user, rows: list[PreferenceRowDTO]) -> list[NotificationPreference]: ...

    @abstractmethod
    def announce(self, data: AnnouncementDTO, *, actor) -> dict[str, Any]: ...


class INotificationTemplateService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[NotificationTemplate]: ...

    @abstractmethod
    def get(self, *, pk: int) -> NotificationTemplate | None: ...

    @abstractmethod
    def create(self, data: CreateTemplateDTO) -> NotificationTemplate: ...

    @abstractmethod
    def update(self, template: NotificationTemplate, changes: dict[str, Any]) -> NotificationTemplate: ...

    @abstractmethod
    def delete(self, template: NotificationTemplate) -> None: ...
