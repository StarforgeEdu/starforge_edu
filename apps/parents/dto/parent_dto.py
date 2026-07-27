"""Parent-domain DTOs (create payloads; updates flow through a validated changes dict)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParentCreateDTO:
    username: str = ""
    phone: str = ""
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    middle_name: str = ""
    birthdate: object | None = None  # datetime.date | None (loose to avoid an import cycle)
    gender: str = ""
    workplace: str = ""
    notes: str = ""


@dataclass(frozen=True)
class GuardianCreateDTO:
    parent_id: int
    student_id: int
    relationship: str
    is_primary: bool = False
    custody_notes: str = ""


@dataclass(frozen=True)
class PickupCreateDTO:
    student_id: int
    full_name: str
    phone: str
    relationship: str = ""
    is_active: bool = True
