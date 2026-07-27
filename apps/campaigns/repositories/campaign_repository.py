"""ORM-backed campaign repository (branch-scoped reads)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.campaigns.interfaces.repositories import ICampaignRepository
from apps.campaigns.models import Campaign, CampaignRecipient
from core.repositories import BaseRepository


class CampaignRepository(BaseRepository[Campaign], ICampaignRepository):
    model = Campaign

    def get_queryset(self) -> QuerySet[Campaign]:
        return Campaign.objects.select_related("branch", "created_by", "sent_by")

    def scoped(self, *, is_unscoped: bool, branch_ids: set[int]) -> QuerySet[Campaign]:
        qs = self.get_queryset()
        if is_unscoped:
            return qs
        return qs.filter(branch_id__in=branch_ids)  # reception sees only its own branch(es)

    def get_scoped(self, *, is_unscoped: bool, branch_ids: set[int], pk: int) -> Campaign | None:
        return self.scoped(is_unscoped=is_unscoped, branch_ids=branch_ids).filter(pk=pk).first()

    def recipients_of(self, campaign: Campaign) -> QuerySet[CampaignRecipient]:
        # student__user so recipient_to_dict can emit student_name without a per-row query.
        return CampaignRecipient.objects.filter(campaign=campaign).select_related("student__user")
