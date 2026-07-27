"""Placement service — thin orchestration over the preserved placement domain
functions (the test-bank lifecycle, auto-grading, group placement) + the base
read repositories. The heavy logic (question validation, grading, level mapping,
maker-checker, AI enqueue, group enroll) stays VERBATIM in
``apps.placement.services`` (the package __init__), imported by the celery
ai_tasks (apply_generated_questions / apply_writing_marks).
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.placement import selectors
from apps.placement import services as domain
from apps.placement.interfaces.repositories import (
    IGroupProposalRepository,
    IPlacementAttemptRepository,
    IPlacementTestRepository,
)
from apps.placement.interfaces.services import IPlacementService
from apps.placement.models import (
    GroupProposal,
    PlacementAttempt,
    PlacementQuestion,
    PlacementTest,
)


class PlacementService(IPlacementService):
    def __init__(
        self,
        test_repository: IPlacementTestRepository,
        attempt_repository: IPlacementAttemptRepository,
        proposal_repository: IGroupProposalRepository,
    ) -> None:
        self._tests = test_repository
        self._attempts = attempt_repository
        self._proposals = proposal_repository

    # --- base querysets ---
    def tests_base(self) -> QuerySet[PlacementTest]:
        return self._tests.base_query()

    def attempts_base(self) -> QuerySet[PlacementAttempt]:
        return self._attempts.base_query()

    def proposals_base(self) -> QuerySet[GroupProposal]:
        return self._proposals.base_query()

    # --- test lifecycle ---
    def create_test(self, *, created_by: Any, **data: Any) -> PlacementTest:
        return domain.create_test(created_by=created_by, **data)

    def update_test(self, *, test: PlacementTest, changes: dict[str, Any]) -> PlacementTest:
        return domain.update_test(test=test, **changes)

    def delete_test(self, *, test: PlacementTest) -> None:
        domain.delete_test(test=test)

    def add_question(self, *, test: PlacementTest, **data: Any) -> PlacementQuestion:
        return domain.add_question(test=test, **data)

    def remove_question(self, *, question: PlacementQuestion) -> None:
        domain.remove_question(question=question)

    def submit_test(self, *, test: PlacementTest) -> PlacementTest:
        return domain.submit_for_review(test=test)

    def approve_test(self, *, test: PlacementTest, approver: Any) -> PlacementTest:
        return domain.approve_test(test=test, approver=approver)

    def reject_test(self, *, test: PlacementTest, reviewer: Any, reason: str) -> PlacementTest:
        return domain.reject_test(test=test, reviewer=reviewer, reason=reason)

    def request_generation(self, *, test: PlacementTest, requested_by: Any, **params: Any) -> Any:
        return domain.request_placement_generation(test=test, requested_by=requested_by, **params)

    # --- attempt lifecycle ---
    def assign(self, *, test: PlacementTest, student: Any, assigned_by: Any) -> PlacementAttempt:
        return domain.assign_test(test=test, student=student, assigned_by=assigned_by)

    def submit_attempt(self, *, attempt: PlacementAttempt, answers: list[dict]) -> PlacementAttempt:
        return domain.submit_attempt(attempt=attempt, answers=answers)

    def request_writing_marking(self, *, attempt: PlacementAttempt, requested_by: Any) -> Any:
        return domain.request_writing_marking(attempt=attempt, requested_by=requested_by)

    def mark_writing_manual(self, *, attempt: PlacementAttempt, marks: list[dict]) -> PlacementAttempt:
        return domain.mark_writing_manually(attempt=attempt, marks=marks)

    def suggestions(self, *, student: Any) -> list[dict]:
        return selectors.suggest_cohorts(student=student)

    # --- group proposals ---
    def propose(self, *, student: Any, cohort: Any, proposed_by: Any) -> GroupProposal:
        return domain.propose_group(student=student, cohort=cohort, proposed_by=proposed_by)

    def accept(self, *, proposal: GroupProposal, manager: Any) -> GroupProposal:
        return domain.accept_proposal(proposal=proposal, manager=manager)

    def reject_proposal(self, *, proposal: GroupProposal, manager: Any, reason: str) -> GroupProposal:
        return domain.reject_proposal(proposal=proposal, manager=manager, reason=reason)
