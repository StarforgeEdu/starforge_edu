"""SubmissionService — retrieve/grade/request-AI-feedback over scoped submissions."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.assignments.interfaces.repositories import ISubmissionRepository
from apps.assignments.interfaces.services import ISubmissionService
from apps.assignments.models import Submission, SubmissionGrade


class SubmissionService(ISubmissionService):
    def __init__(self, submissions: ISubmissionRepository) -> None:
        self._submissions = submissions

    def scoped_list(
        self,
        *,
        user,
        roles: set[str],
        permission: str = "assignments:read",
    ) -> QuerySet[Submission]:
        return self._submissions.scoped(user=user, roles=roles, permission=permission)

    def get_visible(
        self,
        *,
        user,
        roles: set[str],
        pk: int,
        permission: str = "assignments:read",
    ) -> Submission | None:
        return self._submissions.get_scoped(
            user=user,
            roles=roles,
            pk=pk,
            permission=permission,
        )

    def grade(
        self, submission: Submission, *, score, rubric_scores: list, feedback: str, actor
    ) -> SubmissionGrade:
        from apps.assignments.services import grade_submission

        return grade_submission(
            submission=submission, score=score, rubric_scores=rubric_scores, feedback=feedback, actor=actor
        )

    def request_ai_feedback(
        self,
        submission: Submission,
        *,
        requested_by,
        requested_principal=None,
    ) -> None:
        from apps.assignments.services import request_ai_feedback

        request_ai_feedback(
            submission=submission,
            requested_by=requested_by,
            requested_principal=requested_principal,
        )

    def return_for_revision(self, submission: Submission, *, actor) -> Submission:
        from apps.assignments.services import return_submission

        return return_submission(submission=submission, actor=actor)

    def check_plagiarism(self, submission: Submission):
        from apps.assignments.services import check_submission

        return check_submission(submission)
