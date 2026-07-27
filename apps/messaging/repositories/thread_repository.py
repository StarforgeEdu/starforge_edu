"""ORM-backed messaging repository — participant-scoped thread reads."""

from __future__ import annotations

from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet, Subquery

from apps.messaging.interfaces.repositories import IThreadRepository
from apps.messaging.models import Message, Thread, ThreadParticipant
from apps.users.models import RoleMembership, User
from core.repositories import BaseRepository


class ThreadRepository(BaseRepository[Thread], IThreadRepository):
    model = Thread

    def participant_threads(self, *, user) -> QuerySet[Thread]:
        # Strict isolation: only threads the user is a member of are ever resolvable,
        # so every detail/action is participant-gated by construction. `messages` is NOT
        # prefetched: it is append-only/unbounded and was only used to count unread — a
        # page of long threads would load tens of thousands of message rows just to produce
        # a few integers. Unread is now one bounded query (unread_counts). `participants` is
        # small and stays prefetched (the presenter emits the roster).
        return (
            Thread.objects.filter(participants__user_id=user.pk)
            .distinct()
            .select_related("branch", "created_by")
            .prefetch_related("participants")
        )

    def unread_counts(self, *, thread_ids: list[int], viewer_id: int) -> dict[int, int]:
        """{thread_id: unread_count} for `viewer_id` across the given threads in ONE query.

        Unread = messages from OTHERS newer than the viewer's own last_read for that thread
        (a null last_read means everything from others is unread) — the exact semantics the
        old per-row Python count had, but bounded to the page's threads and served by the
        Message(thread, created_at) index instead of loading every message row."""
        if not thread_ids:
            return {}
        viewer_last_read = ThreadParticipant.objects.filter(
            thread_id=OuterRef("thread_id"), user_id=viewer_id
        ).values("last_read_at")[:1]
        rows = (
            Message.objects.filter(thread_id__in=thread_ids)
            .exclude(sender_id=viewer_id)
            .annotate(_viewer_last_read=Subquery(viewer_last_read))
            .filter(Q(_viewer_last_read__isnull=True) | Q(created_at__gt=F("_viewer_last_read")))
            .values("thread_id")
            .annotate(n=Count("id"))
        )
        return {row["thread_id"]: row["n"] for row in rows}

    def get_participant_thread(self, *, user, pk: int) -> Thread | None:
        return self.participant_threads(user=user).filter(pk=pk).first()

    def messages_of(self, *, thread: Thread) -> QuerySet[Message]:
        return Message.objects.filter(thread=thread).select_related("sender")

    def active_members(self, *, ids: list[int]) -> list[User]:
        # Participants must be active members of THIS center — never a membership-less /
        # cross-tenant user row. Exists() (not a role_memberships__isnull filter, which a
        # LEFT JOIN would let membership-less users slip through).
        active_member = RoleMembership.objects.filter(user_id=OuterRef("pk"), revoked_at__isnull=True)
        return list(User.objects.filter(id__in=ids, is_active=True).filter(Exists(active_member)))
