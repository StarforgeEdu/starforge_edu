"""Messaging-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CreateThreadDTO:
    """A new thread. `participant_ids` are validated ints (deduped) in the view; the
    service resolves them to active members of THIS center (unknown -> 400)."""

    participant_ids: list[int]
    subject: str = ""
    first_body: str = ""
    attachments: list = field(default_factory=list)
