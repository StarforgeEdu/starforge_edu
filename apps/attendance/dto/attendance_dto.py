"""Attendance-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MarkEntryDTO:
    """One row of the `mark` payload. `student_id` is resolved to a StudentProfile
    in the service (unknown id -> 400). When `status` is present/late, the service
    recomputes present-vs-late from `arrived_at`; an explicit excused/absent is kept
    verbatim."""

    student_id: int
    status: str
    arrived_at: datetime | None = None
    note: str = ""
