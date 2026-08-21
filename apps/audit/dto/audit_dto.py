"""Audit read-side DTO (the shared list/export filter)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AuditFilterDTO:
    actor: int | None = None
    actor_principal_kind: str | None = None
    actor_principal_id: int | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    ts_from: datetime | None = None
    ts_to: datetime | None = None
    branch: int | None = None
    department: int | None = None
    scope_status: str | None = None
    sensitivity: str | None = None


@dataclass(frozen=True)
class AuditVisibilityDTO:
    """Exact permission-bearing boundaries for one audit read.

    ``organization_wide`` is true only for a superuser or a director membership
    that itself grants ``audit:read``. Branch-wide and department-specific
    memberships remain separate so a department assignment cannot expand to its
    whole branch.
    """

    organization_wide: bool = False
    branch_wide_ids: frozenset[int] = frozenset()
    department_scopes: frozenset[tuple[int, int]] = frozenset()
    compensation_organization_wide: bool = False
    compensation_branch_wide_ids: frozenset[int] = frozenset()
    compensation_department_scopes: frozenset[tuple[int, int]] = frozenset()
