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
    study_month: int = 1
    lesson_cycle_length: int = 12
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
    """Create a typed teacher assignment on a cohort."""

    teacher_id: int
    teacher_type_id: int | None = None
    legacy_role: str = ""


@dataclass(frozen=True)
class TeacherTypeCreateDTO:
    name: str
    slug: str
    description: str = ""
    is_active: bool = True
    is_default: bool = False
    sort_order: int = 100
