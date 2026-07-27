"""Cohort DTOs.

`CohortCreateDTO` is a full frozen create payload; enroll/move carry the small
action inputs. Updates flow through a validated *changes* dict (only the keys in
the PATCH body) so "field absent" stays distinct from "field set to null".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CohortCreateDTO:
    name: str
    branch_id: int
    start_date: date
    end_date: date
    department_id: int | None = None
    level: str = ""
    capacity: int | None = None
    primary_teacher_id: int | None = None
    default_room_id: int | None = None
    is_archived: bool = False


@dataclass(frozen=True)
class CohortEnrollDTO:
    student_id: int
    start_date: date | None = None


@dataclass(frozen=True)
class CohortMoveDTO:
    student_id: int
    reason: str = ""


@dataclass(frozen=True)
class CohortRemoveDTO:
    """Remove a student from a group without moving them elsewhere (groupless)."""

    student_id: int
    reason: str = ""


@dataclass(frozen=True)
class CohortTeacherDTO:
    """Assign a co-teacher/assistant to a cohort (F4)."""

    teacher_id: int
    role: str
