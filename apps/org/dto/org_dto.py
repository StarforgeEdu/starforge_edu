"""Org-domain DTOs (create payloads; updates flow through validated changes dicts)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal


@dataclass(frozen=True)
class BranchCreateDTO:
    name: str
    slug: str
    address: str = ""
    phone: str = ""
    timezone: str = "Asia/Tashkent"
    is_active: bool = True
    max_students: int | None = None
    max_teachers: int | None = None


@dataclass(frozen=True)
class DepartmentCreateDTO:
    branch_id: int
    name: str
    slug: str
    description: str = ""
    is_active: bool = True
    head_id: int | None = None
    budget: Decimal | None = None


@dataclass(frozen=True)
class RoomCreateDTO:
    branch_id: int
    name: str
    capacity: int = 0
    equipment: list = field(default_factory=list)
    is_active: bool = True
    notes: str = ""


@dataclass(frozen=True)
class WorkingHourDTO:
    weekday: int
    opens_at: time
    closes_at: time
    is_closed: bool = False


@dataclass(frozen=True)
class HolidayCreateDTO:
    date: date
    name: str
    is_working_day_override: bool = False
