from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from django.db.models import QuerySet

from apps.crm.dto import (
    AttributionCreateDTO,
    CampaignCreateDTO,
    CRMScope,
    DuplicateReviewDTO,
    FollowUpCreateDTO,
    LeadCreateDTO,
    LeadFilterDTO,
    LeadOwnerDTO,
    PipelineStageDTO,
    StageTransitionDTO,
    TouchCreateDTO,
)
from apps.crm.models import (
    AcquisitionCampaign,
    CRMLead,
    LeadAttribution,
    LeadDuplicateCandidate,
    LeadFollowUp,
    LeadMerge,
    LeadSource,
    LeadStageHistory,
    LeadTouch,
    PipelineStage,
)
from core.role_principals import RolePrincipal


class ICRMService(ABC):
    @abstractmethod
    def stages(self, *, active_only: bool = False) -> QuerySet[PipelineStage]: ...

    @abstractmethod
    def sources(self, *, active_only: bool = False) -> QuerySet[LeadSource]: ...

    @abstractmethod
    def campaigns(self, *, scope: CRMScope, active_only: bool = False) -> QuerySet[AcquisitionCampaign]: ...

    @abstractmethod
    def stage_history(self, lead: CRMLead) -> QuerySet[LeadStageHistory]: ...

    @abstractmethod
    def touches(self, lead: CRMLead) -> QuerySet[LeadTouch]: ...

    @abstractmethod
    def follow_ups(self, lead: CRMLead) -> QuerySet[LeadFollowUp]: ...

    @abstractmethod
    def follow_up_register(self, *, scope: CRMScope) -> QuerySet[LeadFollowUp]: ...

    @abstractmethod
    def attributions(self, lead: CRMLead) -> QuerySet[LeadAttribution]: ...

    @abstractmethod
    def duplicates(self, *, scope: CRMScope) -> QuerySet[LeadDuplicateCandidate]: ...

    @abstractmethod
    def create_stage(
        self,
        data: PipelineStageDTO,
        *,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[PipelineStage, bool]: ...

    @abstractmethod
    def update_stage(
        self,
        stage_id: int,
        changes: dict,
        *,
        actor,
        actor_principal: RolePrincipal,
    ) -> PipelineStage: ...

    @abstractmethod
    def create_source(
        self,
        *,
        slug: str,
        name: str,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadSource, bool]: ...

    @abstractmethod
    def create_campaign(
        self,
        data: CampaignCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[AcquisitionCampaign, bool]: ...

    @abstractmethod
    def leads(self, *, scope: CRMScope, filters: LeadFilterDTO) -> QuerySet[CRMLead]: ...

    @abstractmethod
    def get_lead(self, *, scope: CRMScope, pk: int) -> CRMLead | None: ...

    @abstractmethod
    def create_lead(
        self,
        data: LeadCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[CRMLead, bool]: ...

    @abstractmethod
    def assign_owner(
        self,
        lead_id: int,
        owner: LeadOwnerDTO | None,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[CRMLead, bool]: ...

    @abstractmethod
    def transition(
        self,
        lead_id: int,
        data: StageTransitionDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadStageHistory, bool]: ...

    @abstractmethod
    def add_touch(
        self,
        lead_id: int,
        data: TouchCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadTouch, bool]: ...

    @abstractmethod
    def add_follow_up(
        self,
        lead_id: int,
        data: FollowUpCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadFollowUp, bool]: ...

    @abstractmethod
    def resolve_follow_up(
        self,
        follow_up_id: int,
        *,
        status: str,
        note: str,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadFollowUp, bool]: ...

    @abstractmethod
    def add_attribution(
        self,
        lead_id: int,
        data: AttributionCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadAttribution, bool]: ...

    @abstractmethod
    def detect_duplicates(
        self,
        lead_id: int,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[list[LeadDuplicateCandidate], bool]: ...

    @abstractmethod
    def dismiss_duplicate(
        self,
        candidate_id: int,
        data: DuplicateReviewDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadDuplicateCandidate, bool]: ...

    @abstractmethod
    def merge_duplicate(
        self,
        candidate_id: int,
        data: DuplicateReviewDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadMerge, bool]: ...

    @abstractmethod
    def funnel(
        self,
        *,
        scope: CRMScope,
        date_from: date,
        date_to: date,
        branch_id: int | None,
        department_id: int | None,
        source_id: int | None,
        campaign_id: int | None,
    ) -> dict: ...
