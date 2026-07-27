"""Assignment-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class CreateAssignmentDTO:
    cohort_id: int
    title: str
    due_at: datetime
    description: str = ""
    attachments: list[Any] = field(default_factory=list)
    rubric: list[Any] = field(default_factory=list)
    max_score: Decimal | None = None  # None -> the model default (100)
    max_resubmits: int | None = None
