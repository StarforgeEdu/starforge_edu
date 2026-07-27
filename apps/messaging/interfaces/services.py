"""Messaging-domain service port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.messaging.dto.thread_dto import CreateThreadDTO
from apps.messaging.models import Message, Thread


class IThreadService(ABC):
    @abstractmethod
    def scoped_threads(self, *, user) -> QuerySet[Thread]: ...

    @abstractmethod
    def get_thread(self, *, user, pk: int) -> Thread | None: ...

    @abstractmethod
    def messages_of(self, *, thread: Thread) -> QuerySet[Message]: ...

    @abstractmethod
    def unread_counts(self, *, thread_ids: list[int], viewer_id: int) -> dict[int, int]: ...

    @abstractmethod
    def create(self, data: CreateThreadDTO, *, creator) -> Thread: ...

    @abstractmethod
    def post(self, *, thread: Thread, sender, body: str, attachments: list) -> Message: ...

    @abstractmethod
    def mark_read(self, *, thread: Thread, user) -> None: ...
