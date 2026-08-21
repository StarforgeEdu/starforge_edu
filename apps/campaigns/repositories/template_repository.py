"""ORM-backed message-template repository (unscoped centre-wide templates)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.campaigns.interfaces.repositories import ITemplateRepository
from apps.campaigns.models import MessageTemplate
from core.repositories import BaseRepository


class TemplateRepository(BaseRepository[MessageTemplate], ITemplateRepository):
    model = MessageTemplate

    def get_queryset(self) -> QuerySet[MessageTemplate]:
        return MessageTemplate.objects.select_related("created_by")
