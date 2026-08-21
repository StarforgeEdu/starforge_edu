"""Campaign-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class CreateCampaignDTO:
    name: str
    message: str = ""
    template_id: int | None = None
    branch_id: int | None = None
    segment: dict[str, Any] = field(default_factory=dict)
    scheduled_at: datetime | None = None


@dataclass(frozen=True)
class CreateTemplateDTO:
    name: str
    category: str = ""
    purpose: str = ""
