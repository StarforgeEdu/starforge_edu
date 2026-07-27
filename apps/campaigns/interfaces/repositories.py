"""Campaign-domain repository ports.

Campaigns are BRANCH-scoped (a director sees all; anyone else only their branch(es)').
The do-not-contact list and message templates are UNSCOPED centre-wide tables.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.campaigns.models import Campaign, CampaignRecipient, DoNotContact, MessageTemplate
from core.interfaces import IBaseRepository


class ICampaignRepository(IBaseRepository[Campaign]):
    def scoped(self, *, is_unscoped: bool, branch_ids: set[int]) -> QuerySet[Campaign]:
        raise NotImplementedError

    def get_scoped(self, *, is_unscoped: bool, branch_ids: set[int], pk: int) -> Campaign | None:
        raise NotImplementedError

    def recipients_of(self, campaign: Campaign) -> QuerySet[CampaignRecipient]:
        raise NotImplementedError


class IDoNotContactRepository(IBaseRepository[DoNotContact]): ...


class ITemplateRepository(IBaseRepository[MessageTemplate]): ...
