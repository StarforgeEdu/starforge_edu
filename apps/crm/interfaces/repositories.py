from __future__ import annotations

from django.db.models import QuerySet

from apps.crm.dto import CRMScope, LeadFilterDTO
from apps.crm.models import (
    AcquisitionCampaign,
    CRMLead,
    LeadAttribution,
    LeadDuplicateCandidate,
    LeadFollowUp,
    LeadSource,
    LeadStageHistory,
    LeadTouch,
    PipelineStage,
)
from core.interfaces import IBaseRepository


class ICRMRepository(IBaseRepository[CRMLead]):
    def scoped_leads(self, *, scope: CRMScope, filters: LeadFilterDTO | None = None) -> QuerySet[CRMLead]:
        raise NotImplementedError

    def get_scoped_lead(self, *, scope: CRMScope, pk: int, lock: bool = False) -> CRMLead | None:
        raise NotImplementedError

    def stages(self, *, active_only: bool = False) -> QuerySet[PipelineStage]:
        raise NotImplementedError

    def sources(self, *, active_only: bool = False) -> QuerySet[LeadSource]:
        raise NotImplementedError

    def campaigns(self, *, scope: CRMScope, active_only: bool = False) -> QuerySet[AcquisitionCampaign]:
        raise NotImplementedError

    def stage_history(self, lead: CRMLead) -> QuerySet[LeadStageHistory]:
        raise NotImplementedError

    def touches(self, lead: CRMLead) -> QuerySet[LeadTouch]:
        raise NotImplementedError

    def follow_ups(self, lead: CRMLead) -> QuerySet[LeadFollowUp]:
        raise NotImplementedError

    def scoped_follow_ups(self, *, scope: CRMScope) -> QuerySet[LeadFollowUp]:
        raise NotImplementedError

    def attributions(self, lead: CRMLead) -> QuerySet[LeadAttribution]:
        raise NotImplementedError

    def duplicate_candidates(self, *, scope: CRMScope) -> QuerySet[LeadDuplicateCandidate]:
        raise NotImplementedError

    def get_duplicate_candidate(
        self, *, scope: CRMScope, pk: int, lock: bool = False
    ) -> LeadDuplicateCandidate | None:
        raise NotImplementedError
