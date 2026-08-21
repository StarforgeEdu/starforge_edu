from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class CRMScope:
    organization_wide: bool = False
    branch_wide_ids: frozenset[int] = frozenset()
    department_scopes: frozenset[tuple[int, int]] = frozenset()

    def allows(self, *, branch_id: int, department_id: int | None) -> bool:
        return (
            self.organization_wide
            or branch_id in self.branch_wide_ids
            or (department_id is not None and (branch_id, department_id) in self.department_scopes)
        )


@dataclass(frozen=True, slots=True)
class LeadOwnerDTO:
    principal_kind: str
    principal_id: int


@dataclass(frozen=True, slots=True)
class LeadCreateDTO:
    student_id: int
    stage_id: int
    source_id: int
    department_id: int | None = None
    owner: LeadOwnerDTO | None = None
    campaign_id: int | None = None
    medium: str = ""
    content: str = ""
    attribution_occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StageTransitionDTO:
    stage_id: int
    expected_version: int
    loss_reason: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class TouchCreateDTO:
    channel: str
    direction: str
    summary: str
    outcome: str = ""
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FollowUpCreateDTO:
    due_at: datetime
    purpose: str
    assignee: LeadOwnerDTO | None = None


@dataclass(frozen=True, slots=True)
class AttributionCreateDTO:
    source_id: int
    campaign_id: int | None = None
    medium: str = ""
    content: str = ""
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DuplicateReviewDTO:
    rationale: str
    canonical_lead_id: int | None = None


@dataclass(frozen=True, slots=True)
class PipelineStageDTO:
    slug: str
    name: str
    category: str
    position: int


@dataclass(frozen=True, slots=True)
class CampaignCreateDTO:
    code: str
    name: str
    source_id: int
    branch_id: int | None = None
    department_id: int | None = None
    starts_on: date | None = None
    ends_on: date | None = None


@dataclass(frozen=True, slots=True)
class LeadFilterDTO:
    branch_id: int | None = None
    department_id: int | None = None
    stage_id: int | None = None
    state: str | None = None
    owner_kind: str | None = None
    owner_id: int | None = None
    source_id: int | None = None
    campaign_id: int | None = None
    follow_up_from: datetime | None = None
    follow_up_to: datetime | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
