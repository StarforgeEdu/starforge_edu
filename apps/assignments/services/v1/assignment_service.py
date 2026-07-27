"""AssignmentService — the layered facade over the assignment lifecycle.

Create/update reproduce the old AssignmentSerializer's authoring rules: a non-staff
teacher may only target a cohort they teach (scoped write -> 400), the rubric is
structurally validated (400), and a rubric whose Σ max_points exceeds max_score is
rejected at authoring time (422). Publish/submit/upload route through the preserved
transactional domain functions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.assignments.dto.assignment_dto import CreateAssignmentDTO
from apps.assignments.interfaces.repositories import IAssignmentRepository, ISubmissionRepository
from apps.assignments.interfaces.services import IAssignmentService
from apps.assignments.models import Assignment, Submission
from core.exceptions import UnprocessableEntity, ValidationException
from core.permissions import Role

_DEFAULT_MAX_SCORE = Decimal("100")
_MUTABLE = ("title", "description", "due_at", "attachments", "rubric", "max_score", "max_resubmits")


class AssignmentService(IAssignmentService):
    def __init__(self, assignments: IAssignmentRepository, submissions: ISubmissionRepository) -> None:
        self._assignments = assignments
        self._submissions = submissions

    def scoped_list(self, *, user, roles: set[str]) -> QuerySet[Assignment]:
        return self._assignments.scoped(user=user, roles=roles)

    def get_visible(self, *, user, roles: set[str], pk: int) -> Assignment | None:
        return self._assignments.get_scoped(user=user, roles=roles, pk=pk)

    def create(self, data: CreateAssignmentDTO, *, creator, user, roles: set[str]) -> Assignment:
        cohort = self._resolve_writable_cohort(data.cohort_id, user, roles)
        self._validate_rubric(data.rubric)
        max_score = data.max_score if data.max_score is not None else _DEFAULT_MAX_SCORE
        self._assert_rubric_cap(data.rubric, max_score)
        fields: dict[str, Any] = {
            "cohort": cohort,
            "created_by": creator,
            "title": data.title,
            "description": data.description,
            "due_at": data.due_at,
            "attachments": data.attachments,
            "rubric": data.rubric,
            "max_resubmits": data.max_resubmits,
        }
        if data.max_score is not None:  # else keep the model default
            fields["max_score"] = data.max_score
        return Assignment.objects.create(**fields)

    def update(self, assignment: Assignment, changes: dict[str, Any], *, user, roles: set[str]) -> Assignment:
        if "rubric" in changes:
            self._validate_rubric(changes["rubric"])
        if "cohort" in changes:
            assignment.cohort = self._resolve_writable_cohort(changes["cohort"], user, roles)
        for field in _MUTABLE:
            if field in changes:
                setattr(assignment, field, changes[field])
        # Re-check the sum-cap against the effective (possibly-updated) rubric + max_score.
        self._assert_rubric_cap(assignment.rubric or [], assignment.max_score)
        assignment.save()
        return assignment

    def delete(self, assignment: Assignment) -> None:
        assignment.delete()

    def publish(self, assignment: Assignment, *, actor) -> Assignment:
        from apps.assignments.services import publish_assignment

        return publish_assignment(assignment=assignment, actor=actor)

    def submissions_of(self, assignment: Assignment, *, user, roles: set[str]) -> QuerySet[Submission]:
        return self._submissions.scoped(user=user, roles=roles).filter(assignment=assignment)

    def submit(self, assignment: Assignment, *, student, text: str, attachment_keys: list) -> Submission:
        from apps.assignments.services import submit

        return submit(assignment=assignment, student=student, text=text, attachment_keys=attachment_keys)

    def upload_url(self, *, filename: str, content_type: str, size_bytes: int) -> dict[str, Any]:
        from apps.assignments.services import validate_and_presign_upload

        return validate_and_presign_upload(
            filename=filename, content_type=content_type, size_bytes=size_bytes
        )

    # --- authoring rules (mirror the old AssignmentSerializer) --------------
    @staticmethod
    def _resolve_writable_cohort(cohort_id: int, user, roles: set[str]):
        from apps.assignments.selectors import STAFF_ROLES, _cohorts_taught_by
        from apps.cohorts.models import Cohort

        if getattr(user, "is_superuser", False) or (roles & STAFF_ROLES):
            cohort = Cohort.objects.filter(pk=cohort_id).first()
        elif Role.TEACHER in roles:  # only a cohort they teach
            cohort = Cohort.objects.filter(pk=cohort_id, id__in=_cohorts_taught_by(user)).first()
        else:
            cohort = None
        if cohort is None:
            raise ValidationException(
                _("Invalid cohort."),
                code="validation_error",
                fields={"cohort": ["Not found or not in your scope."]},
            )
        return cohort

    @staticmethod
    def _validate_rubric(rubric) -> None:
        if not isinstance(rubric, list):
            raise ValidationException(
                _("Rubric must be a list of criteria."),
                code="validation_error",
                fields={"rubric": ["Must be a list of criteria."]},
            )
        for row in rubric:
            if not isinstance(row, dict) or "criterion" not in row or "max_points" not in row:
                raise ValidationException(
                    _("Each rubric row needs 'criterion' and 'max_points'."),
                    code="validation_error",
                    fields={"rubric": ["Each row needs 'criterion' and 'max_points'."]},
                )
            if not isinstance(row["criterion"], str) or not str(row["criterion"]).strip():
                raise ValidationException(
                    _("'criterion' must be a non-empty string."),
                    code="validation_error",
                    fields={"rubric": ["'criterion' must be a non-empty string."]},
                )
            if (
                not isinstance(row["max_points"], int)
                or isinstance(row["max_points"], bool)
                or row["max_points"] < 0
            ):
                raise ValidationException(
                    _("'max_points' must be a non-negative integer."),
                    code="validation_error",
                    fields={"rubric": ["'max_points' must be a non-negative integer."]},
                )

    @staticmethod
    def _assert_rubric_cap(rubric: list, max_score) -> None:
        if not rubric or max_score is None:
            return
        rubric_cap = sum(int(row.get("max_points", 0)) for row in rubric)
        if rubric_cap > max_score:
            # 422 (well-formed but unactionable) — mirrors the grade-time code so
            # clients branch uniformly.
            raise UnprocessableEntity(
                _("The rubric's total points exceed the assignment's max score."),
                code="rubric_exceeds_max_score",
                fields={"rubric": [f"Σ max_points {rubric_cap} > max_score {max_score}."]},
            )
