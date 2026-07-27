"""Meeting-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ScheduleMeetingDTO:
    title: str
    starts_at: datetime
    ends_at: datetime
    attendee_ids: list[int]
    agenda: str = ""
    location: str = ""
    branch_id: int | None = None
