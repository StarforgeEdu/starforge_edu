"""Achievement-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CreateAchievementDTO:
    name: str
    scope: str
    description: str = ""
    emoji: str = ""
    cohort_id: int | None = None


@dataclass(frozen=True)
class GrantAchievementDTO:
    student_id: int
    note: str = ""
