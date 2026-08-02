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

from django.db import transaction
from django.db.models import Q, QuerySet, Subquery
from django.utils.translation import gettext_lazy as _

from apps.assignments.dto.assignment_dto import CreateAssignmentDTO
from apps.assignments.interfaces.repositories import IAssignmentRepository, ISubmissionRepository
from apps.assignments.interfaces.services import IAssignmentService
from apps.assignments.models import Assignment, Submission
from core.exceptions import NotFoundException, UnprocessableEntity, ValidationException
from core.permissions import PermissionRoleSet, Role
from core.scoping import (
    permission_membership_is_unscoped,
    permission_membership_scope_q,
    permission_membership_scopes,
    role_membership_scope_q,
)

_DEFAULT_MAX_SCORE = Decimal("100")
_MUTABLE = ("title", "description", "due_at", "attachments", "rubric", "max_score", "max_resubmits")


class AssignmentService(IAssignmentService):
    def __init__(self, assignments: IAssignmentRepository, submissions: ISubmissionRepository) -> None:
        self._assignments = assignments
        self._submissions = submissions

    def scoped_list(
        self,
        *,
        user,
        roles: set[str],
        permission: str = "assignments:read",
    ) -> QuerySet[Assignment]:
        return self._assignments.scoped(user=user, roles=roles, permission=permission)

    def get_visible(
        self,
        *,
        user,
        roles: set[str],
        pk: int,
        permission: str = "assignments:read",
    ) -> Assignment | None:
        return self._assignments.get_scoped(
            user=user,
            roles=roles,
            pk=pk,
            permission=permission,
        )

    def _lock_writable(self, assignment: Assignment, *, user, roles: set[str]) -> Assignment:
        """Lock one row without applying ``FOR UPDATE`` to a DISTINCT scope query."""
        visible_ids = (
            self._assignments.scoped(
                user=user,
                roles=roles,
                permission="assignments:write",
            )
            .filter(pk=assignment.pk)
            .order_by()
            .values("pk")
        )
        locked = Assignment.objects.select_for_update().filter(pk__in=Subquery(visible_ids)).first()
        if locked is None:
            raise NotFoundException(_("Assignment not found."), code="not_found")
        return locked

    @transaction.atomic
    def create(self, data: CreateAssignmentDTO, *, creator, user, roles: set[str]) -> Assignment:
        cohort = self._resolve_writable_cohort(data.cohort_id, user, roles)
        self._validate_rubric(data.rubric)
        max_score = data.max_score if data.max_score is not None else _DEFAULT_MAX_SCORE
        self._validate_numeric_limits(max_score=max_score, max_resubmits=data.max_resubmits)
        self._assert_rubric_cap(data.rubric, max_score)
        fields: dict[str, Any] = {
            "cohort": cohort,
            "created_by": creator,
            "title": data.title,
            "description": data.description,
            "due_at": data.due_at,
            # The target primary key is part of every durable object key.  Save
            # the row first, then promote staging uploads into that namespace.
            "attachments": [],
            "rubric": data.rubric,
            "max_resubmits": data.max_resubmits,
        }
        if data.max_score is not None:  # else keep the model default
            fields["max_score"] = data.max_score
        assignment = Assignment.objects.create(**fields)
        from apps.assignments.services import (
            consume_assignment_attachments,
            discard_promoted_attachment_keys,
        )

        promoted: list[str] = []
        try:
            promoted = consume_assignment_attachments(
                target=assignment,
                keys=data.attachments,
                actor=creator,
            )
            assignment.attachments = promoted
            assignment.save(update_fields=["attachments", "updated_at"])
        except Exception:
            discard_promoted_attachment_keys(promoted)
            raise
        return assignment

    @transaction.atomic
    def update(self, assignment: Assignment, changes: dict[str, Any], *, user, roles: set[str]) -> Assignment:
        # Re-authorize and lock the current row. Without this, two attachment
        # updates can both retain a stale key set, leave the losing promotion
        # orphaned, and overwrite each other's record state.
        assignment = self._lock_writable(assignment, user=user, roles=roles)
        if "rubric" in changes:
            self._validate_rubric(changes["rubric"])
        if "cohort" in changes:
            assignment.cohort = self._resolve_writable_cohort(changes["cohort"], user, roles)
        for field in _MUTABLE:
            if field == "attachments":
                continue
            if field in changes:
                setattr(assignment, field, changes[field])
        self._validate_numeric_limits(
            max_score=assignment.max_score,
            max_resubmits=assignment.max_resubmits,
        )
        # Validate all database-only invariants before any object is copied.
        self._assert_rubric_cap(assignment.rubric or [], assignment.max_score)
        if "attachments" in changes:
            from apps.assignments.services import (
                consume_assignment_attachments,
                discard_promoted_attachment_keys,
                trusted_attachment_keys,
            )

            existing_keys = set(trusted_attachment_keys(assignment))
            promoted: list[str] = []
            try:
                promoted = consume_assignment_attachments(
                    target=assignment,
                    keys=changes["attachments"],
                    actor=user,
                )
                assignment.attachments = promoted
                assignment.save()
            except Exception:
                discard_promoted_attachment_keys([key for key in promoted if key not in existing_keys])
                raise
        else:
            assignment.save()
        return assignment

    @transaction.atomic
    def delete(self, assignment: Assignment, *, user, roles: set[str]) -> None:
        from apps.assignments.services import enqueue_attachment_deletions, trusted_attachment_keys

        assignment = self._lock_writable(assignment, user=user, roles=roles)
        keys = list(trusted_attachment_keys(assignment))
        submissions = list(
            Submission.objects.filter(assignment=assignment)
            .exclude(attachments=[])
            .select_related("student__user")
        )
        for submission in submissions:
            keys.extend(trusted_attachment_keys(submission))
        # Clear references inside the same transaction so cascade signals do
        # not enqueue one storage task per submission. The records are deleted
        # immediately afterward; this is only task fan-out control.
        Assignment.objects.filter(pk=assignment.pk).update(attachments=[])
        Submission.objects.filter(pk__in=[item.pk for item in submissions]).update(attachments=[])
        assignment.attachments = []
        enqueue_attachment_deletions(list(dict.fromkeys(keys)))
        assignment.delete()

    def publish(self, assignment: Assignment, *, actor) -> Assignment:
        from apps.assignments.services import publish_assignment

        return publish_assignment(assignment=assignment, actor=actor)

    def close(self, assignment: Assignment, *, actor) -> Assignment:
        from apps.assignments.services import close_assignment

        return close_assignment(assignment=assignment, actor=actor)

    def submissions_of(
        self,
        assignment: Assignment,
        *,
        user,
        roles: set[str],
        permission: str = "assignments:read",
    ) -> QuerySet[Submission]:
        return self._submissions.scoped(
            user=user,
            roles=roles,
            permission=permission,
        ).filter(assignment=assignment)

    def submit(
        self, assignment: Assignment, *, student, text: str, attachment_keys: list, actor=None
    ) -> Submission:
        from apps.assignments.services import submit

        return submit(
            assignment=assignment,
            student=student,
            text=text,
            attachment_keys=attachment_keys,
            actor=actor,
        )

    def upload_url(
        self, *, filename: str, content_type: str, size_bytes: int, requested_by
    ) -> dict[str, Any]:
        from apps.assignments.services import validate_and_presign_upload

        return validate_and_presign_upload(
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            requested_by=requested_by,
        )

    # --- authoring rules (mirror the old AssignmentSerializer) --------------
    @staticmethod
    def _resolve_writable_cohort(cohort_id: int, user, roles: set[str]):
        from apps.assignments.selectors import STAFF_ROLES, _cohorts_taught_by
        from apps.cohorts.models import Cohort

        permission_unscoped = isinstance(roles, PermissionRoleSet) and permission_membership_is_unscoped(
            roles=roles,
            permission="assignments:write",
            account_kinds={"staff"},
        )
        legacy_unscoped = not isinstance(roles, PermissionRoleSet) and bool(roles & STAFF_ROLES)
        if getattr(user, "is_superuser", False) or permission_unscoped or legacy_unscoped:
            cohort = Cohort.objects.filter(pk=cohort_id).first()
        elif isinstance(roles, PermissionRoleSet):
            teacher_can_write = bool(
                permission_membership_scopes(
                    roles=roles,
                    permission="assignments:write",
                    account_kinds={"teacher"},
                )
            )
            cohort = (
                Cohort.objects.filter(
                    permission_membership_scope_q(
                        roles=roles,
                        permission="assignments:write",
                        branch_field="branch_id",
                        department_field="department_id",
                        account_kinds={"staff"},
                    )
                    | (
                        Q(
                            pk__in=_cohorts_taught_by(
                                user,
                                roles=roles,
                                permission="assignments:write",
                            )
                        )
                        if teacher_can_write
                        else Q(pk__in=[])
                    )
                )
                .filter(pk=cohort_id)
                .first()
            )
        elif Role.HEAD_OF_DEPT in roles:
            cohort = (
                Cohort.objects.filter(
                    role_membership_scope_q(
                        user=user,
                        roles={Role.HEAD_OF_DEPT},
                        branch_field="branch_id",
                        department_field="department_id",
                    )
                )
                .filter(pk=cohort_id)
                .first()
            )
        elif Role.TEACHER in roles:  # only a cohort they teach
            cohort = Cohort.objects.filter(
                pk=cohort_id,
                id__in=_cohorts_taught_by(user, roles=roles, permission="assignments:write"),
            ).first()
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

    @staticmethod
    def _validate_numeric_limits(*, max_score, max_resubmits) -> None:
        if max_score is None or max_score <= 0:
            raise ValidationException(
                _("Invalid max score."),
                code="validation_error",
                fields={"max_score": ["Must be greater than zero."]},
            )
        if max_resubmits is not None and max_resubmits < 0:
            raise ValidationException(
                _("Invalid resubmission limit."),
                code="validation_error",
                fields={"max_resubmits": ["Must be greater than or equal to zero."]},
            )
