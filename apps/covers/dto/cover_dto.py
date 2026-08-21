"""Cover-request DTOs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CreateCoverDTO:
    lesson_id: int
    reason: str = ""
