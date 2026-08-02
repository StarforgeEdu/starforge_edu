from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StatementExportRequestDTO:
    locale: str = "en"
