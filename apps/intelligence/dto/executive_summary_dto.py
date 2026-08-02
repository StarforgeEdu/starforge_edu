"""Immutable request context for the permission-pruned executive snapshot.

The HTTP layer resolves authorization and presentation defaults once, then hands
this DTO to the application service.  Selectors consequently receive explicit,
already-authorized scope boundaries instead of a request object or an ambiguous
set of branch ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, order=True)
class ExecutiveScopeBoundary:
    """One exact branch boundary; ``department_id=None`` means branch-wide."""

    branch_id: int
    department_id: int | None = None


@dataclass(frozen=True)
class ExecutiveScopeLabel:
    id: int
    name: str
    branch_id: int | None = None

    def to_dict(self) -> dict[str, int | str]:
        payload: dict[str, int | str] = {"id": self.id, "name": self.name}
        if self.branch_id is not None:
            payload["branch"] = self.branch_id
        return payload


@dataclass(frozen=True)
class ExecutiveSummaryScope:
    boundaries: tuple[ExecutiveScopeBoundary, ...]
    branches: tuple[ExecutiveScopeLabel, ...]
    departments: tuple[ExecutiveScopeLabel, ...]
    organization_wide: bool
    requested_branch_id: int | None
    requested_department_id: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "organization_wide": self.organization_wide,
            "branches": [branch.to_dict() for branch in self.branches],
            "departments": [department.to_dict() for department in self.departments],
            "applied_filters": {
                "branch": self.requested_branch_id,
                "department": self.requested_department_id,
            },
        }


@dataclass(frozen=True)
class ExecutiveSummaryWindow:
    date_from: date
    date_to: date
    timezone: str

    def to_dict(self) -> dict[str, str]:
        return {
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "timezone": self.timezone,
            "inclusive": "both",
        }


@dataclass(frozen=True)
class ExecutiveSummaryContext:
    generated_at: datetime
    scope: ExecutiveSummaryScope
    window: ExecutiveSummaryWindow
    locale: str
    currency: str
    included_sections: frozenset[str]
    # Role-native identity is part of both authorization and cache identity.
    # ``users.User`` is only a compatibility bridge and may back several role
    # accounts, so personal attention counts must never key or filter on it
    # alone.
    user_id: int
    principal_kind: str
    principal_id: int
