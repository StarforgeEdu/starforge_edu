"""Repository ports for the tenancy (platform control-center) app."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.tenancy.models import Center


class ICenterRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[Center]:
        """Base Center queryset (domains prefetched) for list filtering."""

    @abstractmethod
    def get(self, pk: int) -> Center | None:
        """A single Center by pk (domains prefetched), or None."""
