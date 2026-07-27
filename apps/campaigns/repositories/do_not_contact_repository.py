"""ORM-backed do-not-contact repository (unscoped centre-wide consent list)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.campaigns.interfaces.repositories import IDoNotContactRepository
from apps.campaigns.models import DoNotContact
from core.repositories import BaseRepository


class DoNotContactRepository(BaseRepository[DoNotContact], IDoNotContactRepository):
    model = DoNotContact

    def get_queryset(self) -> QuerySet[DoNotContact]:
        return DoNotContact.objects.select_related("created_by")
