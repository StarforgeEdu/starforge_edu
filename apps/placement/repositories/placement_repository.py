"""Placement repositories — the base querysets (with select_related/prefetch)
for the test bank, attempts, and group proposals."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.placement.interfaces.repositories import (
    IGroupProposalRepository,
    IPlacementAttemptRepository,
    IPlacementTestRepository,
)
from apps.placement.models import GroupProposal, PlacementAttempt, PlacementTest
from core.repositories import BaseRepository


class PlacementTestRepository(BaseRepository[PlacementTest], IPlacementTestRepository):
    model = PlacementTest

    def base_query(self) -> QuerySet[PlacementTest]:
        return PlacementTest.objects.select_related(
            "subject", "branch", "created_by", "approved_by"
        ).prefetch_related("questions")


class PlacementAttemptRepository(BaseRepository[PlacementAttempt], IPlacementAttemptRepository):
    model = PlacementAttempt

    def base_query(self) -> QuerySet[PlacementAttempt]:
        return PlacementAttempt.objects.select_related("test", "student", "student__user").prefetch_related(
            "answers", "test__questions"
        )


class GroupProposalRepository(BaseRepository[GroupProposal], IGroupProposalRepository):
    model = GroupProposal

    def base_query(self) -> QuerySet[GroupProposal]:
        return GroupProposal.objects.select_related("student", "cohort", "proposed_by", "decided_by")
