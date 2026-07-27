"""Messaging-domain repository port.

Strict participant isolation: a user can only ever resolve threads they're a member of,
so every read/detail is participant-gated (an out-of-scope thread simply isn't in the
queryset -> 404). Participants for a new thread must be active members of THIS center.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.messaging.models import Message, Thread
from apps.users.models import User
from core.interfaces import IBaseRepository


class IThreadRepository(IBaseRepository[Thread]):
    def participant_threads(self, *, user) -> QuerySet[Thread]:
        raise NotImplementedError

    def get_participant_thread(self, *, user, pk: int) -> Thread | None:
        raise NotImplementedError

    def messages_of(self, *, thread: Thread) -> QuerySet[Message]:
        raise NotImplementedError

    def unread_counts(self, *, thread_ids: list[int], viewer_id: int) -> dict[int, int]:
        raise NotImplementedError

    def active_members(self, *, ids: list[int]) -> list[User]:
        raise NotImplementedError
