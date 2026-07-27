"""Repository ports for the placement app. Each exposes the base (unscoped)
queryset with the right select_related/prefetch — the views apply the nuanced
role/branch scoping (reproducing the old get_queryset)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.placement.models import GroupProposal, PlacementAttempt, PlacementTest


class IPlacementTestRepository(ABC):
    @abstractmethod
    def base_query(self) -> QuerySet[PlacementTest]: ...


class IPlacementAttemptRepository(ABC):
    @abstractmethod
    def base_query(self) -> QuerySet[PlacementAttempt]: ...


class IGroupProposalRepository(ABC):
    @abstractmethod
    def base_query(self) -> QuerySet[GroupProposal]: ...
