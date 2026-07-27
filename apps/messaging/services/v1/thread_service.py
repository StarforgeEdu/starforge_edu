"""Messaging service — participant-scoped reads + participant resolution, wrapping the
preserved domain fns (create_thread / post_message / mark_read)."""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.messaging.dto.thread_dto import CreateThreadDTO
from apps.messaging.interfaces.repositories import IThreadRepository
from apps.messaging.interfaces.services import IThreadService
from apps.messaging.models import Message, Thread
from apps.messaging.services import create_thread, mark_read, post_message
from core.exceptions import ValidationException


class ThreadService(IThreadService):
    def __init__(self, repository: IThreadRepository) -> None:
        self.repository = repository

    def scoped_threads(self, *, user) -> QuerySet[Thread]:
        return self.repository.participant_threads(user=user)

    def get_thread(self, *, user, pk: int) -> Thread | None:
        return self.repository.get_participant_thread(user=user, pk=pk)

    def messages_of(self, *, thread: Thread) -> QuerySet[Message]:
        return self.repository.messages_of(thread=thread)

    def unread_counts(self, *, thread_ids: list[int], viewer_id: int) -> dict[int, int]:
        return self.repository.unread_counts(thread_ids=thread_ids, viewer_id=viewer_id)

    def create(self, data: CreateThreadDTO, *, creator) -> Thread:
        users = self.repository.active_members(ids=data.participant_ids)
        if len(users) != len(data.participant_ids):
            raise ValidationException(
                _("One or more participants were not found."), code="unknown_participant"
            )
        return create_thread(
            creator=creator,
            participants=users,
            subject=data.subject,
            first_body=data.first_body,
            attachments=data.attachments,
        )

    def post(self, *, thread: Thread, sender, body: str, attachments: list) -> Message:
        return post_message(thread=thread, sender=sender, body=body, attachments=attachments)

    def mark_read(self, *, thread: Thread, user) -> None:
        mark_read(thread=thread, user=user)
