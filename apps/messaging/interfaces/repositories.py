"""Messaging-domain repository port.

Strict participant isolation: a user can only ever resolve threads they're a member of,
so every read/detail is participant-gated (an out-of-scope thread simply isn't in the
queryset -> 404). Participants for a new thread must be active members of THIS center.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.messaging.dto.thread_dto import ThreadEventPageDTO
from apps.messaging.models import Message, Thread
from apps.users.models import User
from core.interfaces import IBaseRepository


class IThreadRepository(IBaseRepository[Thread]):
    def participant_threads(self, *, user, principal_kind: str, principal_id: int) -> QuerySet[Thread]:
        raise NotImplementedError

    def get_participant_thread(
        self, *, user, principal_kind: str, principal_id: int, pk: int
    ) -> Thread | None:
        raise NotImplementedError

    def messages_of(self, *, thread: Thread) -> QuerySet[Message]:
        raise NotImplementedError

    def event_page(
        self,
        *,
        thread: Thread,
        after: int,
        limit: int,
    ) -> ThreadEventPageDTO:
        raise NotImplementedError

    def is_participant(
        self,
        *,
        thread_id: int,
        user_id: int,
        principal_kind: str,
        principal_id: int,
    ) -> bool:
        raise NotImplementedError

    def unread_counts(
        self,
        *,
        thread_ids: list[int],
        viewer_id: int,
        viewer_principal_kind: str,
        viewer_principal_id: int,
    ) -> dict[int, int]:
        raise NotImplementedError

    def active_members(self, *, ids: list[int]) -> list[User]:
        raise NotImplementedError

    def recipient_scope_principals(
        self, *, authorization_context, principals: list
    ) -> set[tuple[int, str, int]]:
        raise NotImplementedError

    def contacts_for(self, *, authorization_context, category: str = "") -> QuerySet[User]:
        raise NotImplementedError

    def is_active_teacher(self, *, authorization_context) -> bool:
        raise NotImplementedError
