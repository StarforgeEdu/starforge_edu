"""Center repository — the only ORM touchpoint for the platform read side."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.tenancy.interfaces.repositories import ICenterRepository
from apps.tenancy.models import Center
from core.repositories import BaseRepository


class CenterRepository(BaseRepository[Center], ICenterRepository):
    model = Center

    def query(self) -> QuerySet[Center]:
        return Center.objects.prefetch_related("domains", "domain_claims").all()

    def get(self, pk: int) -> Center | None:
        return self.query().filter(pk=pk).first()
