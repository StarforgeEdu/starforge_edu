"""Evidence classification for the historical finance attribution backfill."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from core.historical_scope import ScopeAttributionStatus


@dataclass(frozen=True, slots=True)
class ScopeEvidence:
    source: str
    branch_id: int
    department_id: int | None = None
    consistent: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AttributionResolution:
    status: str
    branch_id: int | None
    department_id: int | None
    evidence: tuple[ScopeEvidence, ...]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "branch_id": self.branch_id,
            "department_id": self.department_id,
            "evidence": [item.as_dict() for item in self.evidence],
        }


def resolve_scope_evidence(evidence) -> AttributionResolution:
    """Resolve only unanimous, internally consistent historical evidence."""
    unique = tuple(
        ScopeEvidence(*values)
        for values in sorted(
            {(item.source, item.branch_id, item.department_id, item.consistent) for item in evidence},
            key=lambda values: (
                values[0],
                values[1],
                values[2] is not None,
                values[2] or 0,
                values[3],
            ),
        )
    )
    if not unique:
        return AttributionResolution(
            status=ScopeAttributionStatus.UNRESOLVED,
            branch_id=None,
            department_id=None,
            evidence=(),
        )
    branch_ids = {item.branch_id for item in unique}
    department_ids = {item.department_id for item in unique if item.department_id is not None}
    if any(not item.consistent for item in unique) or len(branch_ids) != 1 or len(department_ids) > 1:
        return AttributionResolution(
            status=ScopeAttributionStatus.CONFLICTING,
            branch_id=None,
            department_id=None,
            evidence=unique,
        )
    return AttributionResolution(
        status=ScopeAttributionStatus.RESOLVED,
        branch_id=next(iter(branch_ids)),
        department_id=next(iter(department_ids), None),
        evidence=unique,
    )
