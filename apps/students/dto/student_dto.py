"""Student-domain DTOs (create + enrollment transition; updates via a changes dict)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from apps.students.models import StudentProfile


@dataclass(frozen=True)
class StudentCreateDTO:
    branch_id: int
    username: str = ""
    phone: str = ""
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    middle_name: str = ""
    birthdate: object | None = None  # datetime.date | None (loose to avoid an import cycle)
    gender: str = ""
    status: str = StudentProfile.Status.LEAD
    academic_level: str = ""
    location: str = ""
    previous_school: str = ""
    medical_notes: str = ""
    emergency_contacts: list = field(default_factory=list)


@dataclass(frozen=True)
class TransitionDTO:
    to_status: str
    reason_code: str = ""
    note: str = ""


@dataclass(frozen=True)
class LeadershipProfileWindowDTO:
    """Inclusive, organization-time window for one leadership profile."""

    date_from: date
    date_to: date


@dataclass(frozen=True)
class LeadershipProfileAccessDTO:
    """Sections proven accessible for this exact student and request scope."""

    academics: bool = False
    assignments: bool = False
    attendance: bool = False
    teachers: bool = False
    family: bool = False
    safeguarding: bool = False
    finance: bool = False
