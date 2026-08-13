"""Messaging-domain service port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.messaging.dto.thread_dto import CreateThreadDTO, ThreadEventPageDTO, ThreadReadStateDTO
from apps.messaging.models import Message, Thread
from apps.users.models import User


class IThreadService(ABC):
    @abstractmethod
    def scoped_threads(self, *, user, principal_kind: str, principal_id: int) -> QuerySet[Thread]: ...

    @abstractmethod
    def get_thread(self, *, user, principal_kind: str, principal_id: int, pk: int) -> Thread | None: ...

    @abstractmethod
    def messages_of(self, *, thread: Thread) -> QuerySet[Message]: ...

    @abstractmethod
    def get_message(
        self,
        *,
        user,
        principal_kind: str,
        principal_id: int,
        pk: int,
    ) -> Message | None: ...

    @abstractmethod
    def event_page(
        self,
        *,
        thread: Thread,
        after: int,
        limit: int,
    ) -> ThreadEventPageDTO: ...

    @abstractmethod
    def can_stream_thread(
        self,
        *,
        thread_id: int,
        user,
        principal_kind: str,
        principal_id: int,
    ) -> bool: ...

    @abstractmethod
    def unread_counts(
        self,
        *,
        thread_ids: list[int],
        viewer_id: int,
        viewer_principal_kind: str,
        viewer_principal_id: int,
    ) -> dict[int, int]: ...

    @abstractmethod
    def contacts(self, *, authorization_context, category: str = "") -> QuerySet[User]: ...

    @abstractmethod
    def create(self, data: CreateThreadDTO, *, authorization_context) -> Thread: ...

    @abstractmethod
    def post(
        self,
        *,
        thread: Thread,
        sender,
        sender_principal_kind: str,
        sender_principal_id: int,
        body: str,
        attachments: list,
    ) -> Message: ...

    @abstractmethod
    def edit_message(
        self,
        *,
        message: Message,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
        body: str,
        expected_version: int | None,
    ) -> Message: ...

    @abstractmethod
    def delete_message(
        self,
        *,
        message: Message,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
    ) -> Message: ...

    @abstractmethod
    def add_reaction(
        self,
        *,
        message: Message,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
        emoji: str,
    ) -> Message: ...

    @abstractmethod
    def remove_reaction(
        self,
        *,
        message: Message,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
        emoji: str,
    ) -> Message: ...

    @abstractmethod
    def mark_read(
        self,
        *,
        thread: Thread,
        user,
        principal_kind: str,
        principal_id: int,
        through_message_id: int | None,
    ) -> ThreadReadStateDTO: ...

    @abstractmethod
    def set_notifications_muted(
        self,
        *,
        thread: Thread,
        user,
        principal_kind: str,
        principal_id: int,
        muted: bool,
    ) -> None: ...

    @abstractmethod
    def set_archived(
        self,
        *,
        thread: Thread,
        user,
        principal_kind: str,
        principal_id: int,
        archived: bool,
    ) -> None: ...

    @abstractmethod
    def hide_thread(self, *, thread: Thread, user, principal_kind: str, principal_id: int) -> None: ...

    @abstractmethod
    def presign_attachment(
        self, *, filename: str, content_type: str, size_bytes: int, requested_by
    ) -> dict: ...

    @abstractmethod
    def attachment_download_url(self, *, thread: Thread, key: str) -> str: ...
