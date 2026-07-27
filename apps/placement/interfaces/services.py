"""Service port for the placement app."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.placement.models import (
    GroupProposal,
    PlacementAttempt,
    PlacementQuestion,
    PlacementTest,
)


class IPlacementService(ABC):
    # --- base querysets (views apply role/branch scoping) ---
    @abstractmethod
    def tests_base(self) -> QuerySet[PlacementTest]: ...

    @abstractmethod
    def attempts_base(self) -> QuerySet[PlacementAttempt]: ...

    @abstractmethod
    def proposals_base(self) -> QuerySet[GroupProposal]: ...

    # --- test lifecycle (delegate to preserved domain fns) ---
    @abstractmethod
    def create_test(self, *, created_by: Any, **data: Any) -> PlacementTest: ...

    @abstractmethod
    def update_test(self, *, test: PlacementTest, changes: dict[str, Any]) -> PlacementTest: ...

    @abstractmethod
    def delete_test(self, *, test: PlacementTest) -> None: ...

    @abstractmethod
    def add_question(self, *, test: PlacementTest, **data: Any) -> PlacementQuestion: ...

    @abstractmethod
    def remove_question(self, *, question: PlacementQuestion) -> None: ...

    @abstractmethod
    def submit_test(self, *, test: PlacementTest) -> PlacementTest: ...

    @abstractmethod
    def approve_test(self, *, test: PlacementTest, approver: Any) -> PlacementTest: ...

    @abstractmethod
    def reject_test(self, *, test: PlacementTest, reviewer: Any, reason: str) -> PlacementTest: ...

    @abstractmethod
    def request_generation(self, *, test: PlacementTest, requested_by: Any, **params: Any) -> Any: ...

    # --- attempt lifecycle ---
    @abstractmethod
    def assign(self, *, test: PlacementTest, student: Any, assigned_by: Any) -> PlacementAttempt: ...

    @abstractmethod
    def submit_attempt(self, *, attempt: PlacementAttempt, answers: list[dict]) -> PlacementAttempt: ...

    @abstractmethod
    def request_writing_marking(self, *, attempt: PlacementAttempt, requested_by: Any) -> Any: ...

    @abstractmethod
    def mark_writing_manual(self, *, attempt: PlacementAttempt, marks: list[dict]) -> PlacementAttempt: ...

    @abstractmethod
    def suggestions(self, *, student: Any) -> list[dict]: ...

    # --- group proposals ---
    @abstractmethod
    def propose(self, *, student: Any, cohort: Any, proposed_by: Any) -> GroupProposal: ...

    @abstractmethod
    def accept(self, *, proposal: GroupProposal, manager: Any) -> GroupProposal: ...

    @abstractmethod
    def reject_proposal(self, *, proposal: GroupProposal, manager: Any, reason: str) -> GroupProposal: ...
