"""TemplateService — reusable message templates (F10-2), incl. async AI drafting."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.campaigns.dto.campaign_dto import CreateTemplateDTO
from apps.campaigns.interfaces.repositories import ITemplateRepository
from apps.campaigns.interfaces.services import ITemplateService
from apps.campaigns.models import MessageTemplate


class TemplateService(ITemplateService):
    def __init__(self, templates: ITemplateRepository) -> None:
        self._templates = templates

    def list(self) -> QuerySet[MessageTemplate]:
        return self._templates.get_queryset()

    def get(self, pk: int) -> MessageTemplate | None:
        return self._templates.get_by_id(pk)

    def create(self, data: CreateTemplateDTO, *, creator) -> MessageTemplate:
        from apps.campaigns.services import create_template

        return create_template(
            name=data.name, category=data.category, purpose=data.purpose, created_by=creator
        )

    def update(self, template: MessageTemplate, changes: dict[str, Any]) -> MessageTemplate:
        from apps.campaigns.services import update_template

        return update_template(template_id=template.pk, fields=changes)

    def generate(self, template: MessageTemplate, *, requested_by) -> Any:
        from apps.campaigns.services import request_template_generation

        return request_template_generation(template=template, requested_by=requested_by)
