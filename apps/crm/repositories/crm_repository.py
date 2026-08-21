from __future__ import annotations

from django.db.models import DateTimeField, OuterRef, Q, QuerySet, Subquery

from apps.crm.dto import CRMScope, LeadFilterDTO
from apps.crm.interfaces.repositories import ICRMRepository
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
from core.repositories import BaseRepository


def crm_scope_q(scope: CRMScope, *, prefix: str = "") -> Q:
    if scope.organization_wide:
        return Q(**{f"{prefix}pk__isnull": False})
    condition = Q(pk__in=[])
    if scope.branch_wide_ids:
        condition |= Q(**{f"{prefix}branch_id__in": scope.branch_wide_ids})
    for branch_id, department_id in scope.department_scopes:
        condition |= Q(
            **{
                f"{prefix}branch_id": branch_id,
                f"{prefix}department_id": department_id,
            }
        )
    return condition


class CRMRepository(BaseRepository[CRMLead], ICRMRepository):
    model = CRMLead

    def get_queryset(self) -> QuerySet[CRMLead]:
        pending_follow_up = (
            LeadFollowUp.objects.filter(lead_id=OuterRef("pk"), status=LeadFollowUp.Status.PENDING)
            .order_by("due_at", "id")
            .values("due_at")[:1]
        )
        return CRMLead.objects.select_related(
            "student",
            "branch",
            "department",
            "stage",
            "owner",
            "owner__staff_profile",
            "owner__teacher_profile",
            "initial_source",
            "initial_campaign",
            "canonical_lead",
        ).annotate(next_follow_up_at=Subquery(pending_follow_up, output_field=DateTimeField()))

    def scoped_leads(self, *, scope: CRMScope, filters: LeadFilterDTO | None = None) -> QuerySet[CRMLead]:
        qs = self.get_queryset().filter(crm_scope_q(scope))
        if filters is None:
            return qs
        if filters.branch_id is not None:
            qs = qs.filter(branch_id=filters.branch_id)
        if filters.department_id is not None:
            qs = qs.filter(department_id=filters.department_id)
        if filters.stage_id is not None:
            qs = qs.filter(stage_id=filters.stage_id)
        if filters.state is not None:
            qs = qs.filter(state=filters.state)
        if filters.owner_kind is not None and filters.owner_id is not None:
            qs = qs.filter(
                owner_principal_kind=filters.owner_kind,
                owner_principal_id=filters.owner_id,
            )
        if filters.source_id is not None:
            qs = qs.filter(attributions__source_id=filters.source_id)
        if filters.campaign_id is not None:
            qs = qs.filter(attributions__campaign_id=filters.campaign_id)
        if filters.follow_up_from is not None:
            # ``next_follow_up_at`` is the typed Subquery annotation installed
            # by ``get_queryset``; django-stubs only knows concrete model fields.
            qs = qs.filter(next_follow_up_at__gte=filters.follow_up_from)  # type: ignore[misc]
        if filters.follow_up_to is not None:
            qs = qs.filter(next_follow_up_at__lte=filters.follow_up_to)  # type: ignore[misc]
        if filters.created_from is not None:
            qs = qs.filter(created_at__gte=filters.created_from)
        if filters.created_to is not None:
            qs = qs.filter(created_at__lte=filters.created_to)
        return qs.distinct()

    def get_scoped_lead(self, *, scope: CRMScope, pk: int, lock: bool = False) -> CRMLead | None:
        qs = self.get_queryset().filter(crm_scope_q(scope), pk=pk)
        if lock:
            qs = qs.select_for_update(of=("self",))
        return qs.first()

    def stages(self, *, active_only: bool = False) -> QuerySet[PipelineStage]:
        qs = PipelineStage.objects.all()
        return qs.filter(is_active=True) if active_only else qs

    def sources(self, *, active_only: bool = False) -> QuerySet[LeadSource]:
        qs = LeadSource.objects.all()
        return qs.filter(is_active=True) if active_only else qs

    def campaigns(self, *, scope: CRMScope, active_only: bool = False) -> QuerySet[AcquisitionCampaign]:
        qs = AcquisitionCampaign.objects.select_related("source", "branch", "department").filter(
            Q(branch__isnull=True) | crm_scope_q(scope)
        )
        return qs.filter(is_active=True) if active_only else qs

    def stage_history(self, lead: CRMLead) -> QuerySet[LeadStageHistory]:
        return LeadStageHistory.objects.filter(lead=lead).select_related("from_stage", "to_stage", "actor")

    def touches(self, lead: CRMLead) -> QuerySet[LeadTouch]:
        return LeadTouch.objects.filter(lead=lead).select_related("actor")

    def follow_ups(self, lead: CRMLead) -> QuerySet[LeadFollowUp]:
        return self.scoped_follow_ups(scope=CRMScope(organization_wide=True)).filter(lead=lead)

    def scoped_follow_ups(self, *, scope: CRMScope) -> QuerySet[LeadFollowUp]:
        return LeadFollowUp.objects.select_related(
            "lead",
            "lead__student",
            "lead__branch",
            "lead__department",
            "assignee",
            "assignee__staff_profile",
            "assignee__teacher_profile",
            "created_by",
            "created_by__staff_profile",
            "created_by__teacher_profile",
            "resolved_by",
            "resolved_by__staff_profile",
            "resolved_by__teacher_profile",
        ).filter(crm_scope_q(scope, prefix="lead__"))

    def attributions(self, lead: CRMLead) -> QuerySet[LeadAttribution]:
        return LeadAttribution.objects.filter(lead=lead).select_related("source", "campaign", "actor")

    def duplicate_candidates(self, *, scope: CRMScope) -> QuerySet[LeadDuplicateCandidate]:
        return LeadDuplicateCandidate.objects.select_related(
            "left__student", "right__student", "left__branch", "right__branch", "reviewed_by"
        ).filter(crm_scope_q(scope, prefix="left__"), crm_scope_q(scope, prefix="right__"))

    def get_duplicate_candidate(
        self, *, scope: CRMScope, pk: int, lock: bool = False
    ) -> LeadDuplicateCandidate | None:
        qs = self.duplicate_candidates(scope=scope).filter(pk=pk)
        if lock:
            qs = qs.select_for_update(of=("self",))
        return qs.first()
