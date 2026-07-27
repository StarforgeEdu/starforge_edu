"""SubmissionService — retrieve/grade/request-AI-feedback over scoped submissions."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.assignments.interfaces.repositories import ISubmissionRepository
from apps.assignments.interfaces.services import ISubmissionService
from apps.assignments.models import Submission, SubmissionGrade


class SubmissionService(ISubmissionService):
    def __init__(self, submissions: ISubmissionRepository) -> None:
        self._submissions = submissions

    def scoped_list(self, *, user, roles: set[str]) -> QuerySet[Submission]:
        return self._submissions.scoped(user=user, roles=roles)

    def get_visible(self, *, user, roles: set[str], pk: int) -> Submission | None:
        return self._submissions.get_scoped(user=user, roles=roles, pk=pk)

    def grade(
        self, submission: Submission, *, score, rubric_scores: list, feedback: str, actor
    ) -> SubmissionGrade:
        from apps.assignments.services import grade_submission

        return grade_submission(
            submission=submission, score=score, rubric_scores=rubric_scores, feedback=feedback, actor=actor
        )

    def request_ai_feedback(self, submission: Submission, *, requested_by) -> None:
        from apps.assignments.services import request_ai_feedback

        request_ai_feedback(submission=submission, requested_by=requested_by)
