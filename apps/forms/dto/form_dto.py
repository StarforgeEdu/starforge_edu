"""Forms-engine DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class CreateFormDTO:
    title: str
    description: str = ""
    is_anonymous: bool = False
    allow_multiple: bool = False
    branch_id: int | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    audience_roles: list[str] = field(default_factory=list)
    audience_user_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class AddFieldDTO:
    label: str
    field_type: str
    required: bool = False
    order: int | None = None
    options: list[str] = field(default_factory=list)
    help_text: str = ""
