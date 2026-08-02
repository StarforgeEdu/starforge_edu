"""Messaging-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from apps.messaging.models import Message, ThreadRealtimeEvent


@dataclass(frozen=True)
class CreateThreadDTO:
    """A new thread. `participant_ids` are validated ints (deduped) in the view; the
    service resolves them to active members of THIS center (unknown -> 400)."""

    participant_ids: list[int]
    subject: str = ""
    first_body: str = ""
    attachments: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ThreadEventPageDTO:
    """One bounded, ordered recovery page from a thread's durable event log."""

    events: tuple[ThreadRealtimeEvent, ...]
    requested_after: int
    next_cursor: int
    high_watermark: int
    recovery_floor: int
    has_more: bool
    reset_required: bool


@dataclass(frozen=True, slots=True)
class ThreadReadStateDTO:
    """Committed inclusive read position for one exact thread principal."""

    changed: bool
    through_message: Message | None
    read_at: datetime | None
    event: ThreadRealtimeEvent | None
