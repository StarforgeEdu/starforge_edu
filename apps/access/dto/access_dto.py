"""Access-config DTOs (A-2 permission overrides)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OverrideDTO:
    role: str
    permission: str
    effect: str
    note: str = ""
