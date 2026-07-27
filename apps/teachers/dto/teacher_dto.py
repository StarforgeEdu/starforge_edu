"""Teacher DTOs. Create is a full frozen DTO; update is a validated *changes* dict
(only the keys present in the PATCH body), so "department not provided" stays distinct
from "department set to null" — both are real PATCH operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class TeacherCreateDTO:
    branch_id: int
    username: str = ""
    phone: str = ""
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    middle_name: str = ""
    birthdate: date | None = None
    gender: str = ""
    department_id: int | None = None
    hire_date: date | None = None
    subjects: list = field(default_factory=list)
    qualifications: str = ""
    salary_type: str = "monthly"
    rate: Decimal | None = None
    is_substitute: bool = False
