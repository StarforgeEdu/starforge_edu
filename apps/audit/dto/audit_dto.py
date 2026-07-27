"""Audit read-side DTO (the shared list/export filter)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AuditFilterDTO:
    actor: int | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    ts_from: datetime | None = None
    ts_to: datetime | None = None
