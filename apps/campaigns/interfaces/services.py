"""Campaign-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.campaigns.dto.campaign_dto import CreateCampaignDTO, CreateTemplateDTO
from apps.campaigns.models import Campaign, CampaignRecipient, DoNotContact, MessageTemplate


class ICampaignService(ABC):
    @abstractmethod
    def scoped_list(self, *, is_unscoped: bool, branch_ids: set[int]) -> QuerySet[Campaign]: ...

    @abstractmethod
    def get_visible(self, *, is_unscoped: bool, branch_ids: set[int], pk: int) -> Campaign | None: ...

    @abstractmethod
    def create(
        self, data: CreateCampaignDTO, *, creator, is_unscoped: bool, branch_ids: set[int]
    ) -> Campaign: ...

    @abstractmethod
    def send(self, *, campaign_id: int, actor) -> Campaign: ...

    @abstractmethod
    def recipients_of(self, campaign: Campaign) -> QuerySet[CampaignRecipient]: ...


class IDoNotContactService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[DoNotContact]: ...

    @abstractmethod
    def get(self, pk: int) -> DoNotContact | None: ...

    @abstractmethod
    def create(self, *, phone: str, reason: str, actor) -> DoNotContact: ...

    @abstractmethod
    def delete(self, entry: DoNotContact) -> None: ...


class ITemplateService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[MessageTemplate]: ...

    @abstractmethod
    def get(self, pk: int) -> MessageTemplate | None: ...

    @abstractmethod
    def create(self, data: CreateTemplateDTO, *, creator) -> MessageTemplate: ...

    @abstractmethod
    def update(self, template: MessageTemplate, changes: dict[str, Any]) -> MessageTemplate: ...

    @abstractmethod
    def generate(self, template: MessageTemplate, *, requested_by) -> Any: ...
