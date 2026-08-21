"""CampaignService — the layered facade over the SMS-campaign domain functions.

Read scoping is delegated to the repository; build (create_campaign) + send
(send_campaign) route through the transactional domain functions. Branch containment
(a non-director may only run a campaign for their own branch; only the director runs a
centre-wide one) and the message-OR-template resolution live here.
"""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.campaigns.dto.campaign_dto import CreateCampaignDTO
from apps.campaigns.interfaces.repositories import ICampaignRepository, ITemplateRepository
from apps.campaigns.interfaces.services import ICampaignService
from apps.campaigns.models import Campaign, CampaignRecipient
from core.exceptions import PermissionException, ValidationException


class CampaignService(ICampaignService):
    def __init__(self, campaigns: ICampaignRepository, templates: ITemplateRepository) -> None:
        self._campaigns = campaigns
        self._templates = templates

    def scoped_list(self, *, is_unscoped: bool, branch_ids: set[int]) -> QuerySet[Campaign]:
        return self._campaigns.scoped(is_unscoped=is_unscoped, branch_ids=branch_ids)

    def get_visible(self, *, is_unscoped: bool, branch_ids: set[int], pk: int) -> Campaign | None:
        return self._campaigns.get_scoped(is_unscoped=is_unscoped, branch_ids=branch_ids, pk=pk)

    def create(
        self, data: CreateCampaignDTO, *, creator, is_unscoped: bool, branch_ids: set[int]
    ) -> Campaign:
        from apps.campaigns.services import create_campaign

        # Body validation (400) BEFORE the branch-scope check (403) — matches the old
        # serializer-then-perform_create ordering.
        branch = self._resolve_branch(data.branch_id)
        message = self._resolve_message(data)
        self._assert_branch_in_scope(is_unscoped, branch, branch_ids)
        return create_campaign(
            name=data.name,
            message=message,
            segment=data.segment,
            created_by=creator,
            branch=branch,
            scheduled_at=data.scheduled_at,
        )

    def send(self, *, campaign_id: int, actor) -> Campaign:
        from apps.campaigns.services import send_campaign

        return send_campaign(campaign_id=campaign_id, actor=actor)

    def recipients_of(self, campaign: Campaign) -> QuerySet[CampaignRecipient]:
        return self._campaigns.recipients_of(campaign)

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _assert_branch_in_scope(is_unscoped: bool, branch, branch_ids: set[int]) -> None:
        if is_unscoped:
            return  # the director may run a centre-wide or any-branch campaign
        if branch is None:
            raise PermissionException(_("Choose a branch for the campaign."), code="branch_required")
        if branch.id not in branch_ids:
            raise PermissionException(
                _("You can only run a campaign for your own branch."), code="branch_out_of_scope"
            )

    def _resolve_message(self, data: CreateCampaignDTO) -> str:
        """Exactly one source of text: a typed message OR an active template's body."""
        message = (data.message or "").strip()
        template = None
        if data.template_id is not None:
            template = self._templates.get_by_id(data.template_id)
            if template is None or not template.is_active:  # matches the old is_active queryset
                raise ValidationException(
                    _("Invalid template."),
                    code="validation_error",
                    fields={"template": ["Not found or inactive."]},
                )
        if message and template is not None:
            raise ValidationException(
                _("Provide a message OR a template, not both."), code="validation_error"
            )
        if not message and template is None:
            raise ValidationException(_("Provide a message or pick a template."), code="validation_error")
        if template is not None and not (template.body or "").strip():
            raise ValidationException(_("That template has no body yet."), code="validation_error")
        return template.body if template is not None else message

    @staticmethod
    def _resolve_branch(branch_id: int | None):
        if branch_id is None:
            return None
        from apps.org.models import Branch

        branch = Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
        if branch is None:  # mirrors the old serializer's non-archived branch queryset
            raise ValidationException(
                _("Invalid branch."), code="validation_error", fields={"branch": ["Not found."]}
            )
        return branch
