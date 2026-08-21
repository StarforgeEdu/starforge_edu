"""Exact source ownership and live authorization for AI work.

AI jobs cross two trust boundaries: an HTTP request becomes durable background
work, then tenant data leaves for a paid model provider.  This module captures
the source's immutable branch/department ownership at enqueue time and validates
the exact role-native principal both then and immediately before provider use.

No source identifier is accepted as an untyped generic foreign key.  Every AI
feature has one reviewed source model and one permission that authorizes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from django.db.models import Q

from apps.ai.models import AIFeature, AIRequest
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.permissions import get_user_authorization_context
from core.role_principals import (
    RolePrincipal,
    resolve_unambiguous_user_principal,
    validate_role_principal,
)
from core.scoping import permission_membership_is_unscoped, permission_membership_scopes
from core.utils import stable_hash


@dataclass(frozen=True, slots=True)
class AISourceAuthorization:
    permission: str
    scope_status: str
    branch_id: int | None
    department_id: int | None


_FEATURE_SOURCE_APP: dict[str, str] = {
    AIFeature.ASSIGNMENT_FEEDBACK: "assignments",
    AIFeature.EXAM_GENERATION: "academics",
    AIFeature.CONTENT_SUMMARY: "content",
    AIFeature.PLACEMENT_GENERATION: "placement",
    AIFeature.FORM_ANALYSIS: "forms",
    AIFeature.WRITING_MARKING: "placement",
    AIFeature.MATERIAL_GENERATION: "content",
    AIFeature.TEMPLATE_GENERATION: "campaigns",
}

_FEATURE_PERMISSION: dict[str, str] = {
    # A student owns a submission through ``assignments:submit``.  Teacher and
    # manager wildcard grants cover the same concrete permission.
    AIFeature.ASSIGNMENT_FEEDBACK: "assignments:submit",
    AIFeature.EXAM_GENERATION: "ai:write",
    AIFeature.CONTENT_SUMMARY: "content:write",
    AIFeature.PLACEMENT_GENERATION: "placement:write",
    AIFeature.FORM_ANALYSIS: "forms:write",
    AIFeature.WRITING_MARKING: "placement:write",
    AIFeature.MATERIAL_GENERATION: "content:write",
    AIFeature.TEMPLATE_GENERATION: "campaign:write",
}

_FEATURE_SOURCE_PARAM: dict[str, str] = {
    AIFeature.EXAM_GENERATION: "subject_id",
    AIFeature.PLACEMENT_GENERATION: "test_id",
    AIFeature.FORM_ANALYSIS: "form_id",
    AIFeature.WRITING_MARKING: "attempt_id",
    AIFeature.MATERIAL_GENERATION: "material_id",
    AIFeature.TEMPLATE_GENERATION: "template_id",
}


def parameter_fingerprint(params: dict[str, Any] | None) -> str:
    """Canonical, bounded identity for broker-carried task parameters."""

    try:
        payload = json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationException("Invalid AI request parameters.", code="invalid_ai_parameters") from exc
    if len(payload) > 32_000:
        raise ValidationException("AI request parameters are too large.", code="invalid_ai_parameters")
    return stable_hash(payload)


def feature_parameter_fingerprint(*, feature: str, params: dict[str, Any] | None) -> str:
    """Bind only the reviewed, feature-specific broker parameters.

    Source identifiers are already immutable columns on ``AIRequest`` and are
    removed from the broker payload identity. Unknown task keys are rejected so a
    compromised producer cannot smuggle an unreviewed control into a worker.
    """

    try:
        raw = dict(params or {})
    except (TypeError, ValueError) as exc:
        raise ValidationException(
            "Invalid AI request parameters.",
            code="invalid_ai_parameters",
        ) from exc
    source_key = _FEATURE_SOURCE_PARAM.get(feature)
    if source_key is not None:
        raw.pop(source_key, None)
    allowed_by_feature: dict[str, set[str]] = {
        AIFeature.ASSIGNMENT_FEEDBACK: set(),
        AIFeature.CONTENT_SUMMARY: set(),
        AIFeature.EXAM_GENERATION: {"exam_type", "question_count", "difficulty"},
        AIFeature.PLACEMENT_GENERATION: {"count", "difficulty", "topic"},
        AIFeature.FORM_ANALYSIS: set(),
        AIFeature.WRITING_MARKING: set(),
        AIFeature.MATERIAL_GENERATION: {"title", "topic"},
        AIFeature.TEMPLATE_GENERATION: {"name", "purpose"},
    }
    allowed = allowed_by_feature.get(feature)
    if allowed is None or set(raw) != allowed:
        raise ValidationException("Invalid AI request parameters.", code="invalid_ai_parameters")
    valid = True
    if feature == AIFeature.EXAM_GENERATION:
        valid = (
            isinstance(raw["exam_type"], str)
            and 1 <= len(raw["exam_type"]) <= 32
            and isinstance(raw["question_count"], int)
            and not isinstance(raw["question_count"], bool)
            and 1 <= raw["question_count"] <= 100
            and isinstance(raw["difficulty"], str)
            and raw["difficulty"] in {"easy", "medium", "hard"}
        )
    elif feature == AIFeature.PLACEMENT_GENERATION:
        valid = (
            isinstance(raw["count"], int)
            and not isinstance(raw["count"], bool)
            and 1 <= raw["count"] <= 50
            and isinstance(raw["difficulty"], str)
            and raw["difficulty"] in {"easy", "medium", "hard"}
            and isinstance(raw["topic"], str)
            and len(raw["topic"]) <= 200
        )
    elif feature == AIFeature.MATERIAL_GENERATION:
        valid = (
            isinstance(raw["title"], str)
            and 1 <= len(raw["title"]) <= 200
            and isinstance(raw["topic"], str)
            and len(raw["topic"]) <= 500
        )
    elif feature == AIFeature.TEMPLATE_GENERATION:
        valid = (
            isinstance(raw["name"], str)
            and 1 <= len(raw["name"]) <= 120
            and isinstance(raw["purpose"], str)
            and len(raw["purpose"]) <= 500
        )
    if not valid:
        raise ValidationException("Invalid AI request parameters.", code="invalid_ai_parameters")
    return parameter_fingerprint(raw)


def worker_parameters_match_request(*, request: AIRequest, params: dict[str, Any] | None) -> bool:
    """Bind broker-carried source identifiers to the immutable request row.

    Source IDs are intentionally not part of the parameter fingerprint because
    they already live in ``AIRequest.source_id``.  Removing them from the hash is
    safe only when the worker compares the broker value to that column first;
    otherwise a tampered message could read or mutate a second source under the
    authorization snapshot of the first.
    """

    try:
        raw = dict(params or {})
    except (TypeError, ValueError):
        return False
    source_key = _FEATURE_SOURCE_PARAM.get(request.feature)
    if source_key is not None:
        source_id = raw.get(source_key)
        if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id != request.source_id:
            return False
    try:
        fingerprint = feature_parameter_fingerprint(feature=request.feature, params=raw)
    except (TypeError, ValueError, ValidationException):
        return False
    if request.parameter_fingerprint != fingerprint:
        return False
    if request.feature == AIFeature.MATERIAL_GENERATION:
        from apps.content.models import LibraryMaterial

        return LibraryMaterial.objects.filter(
            pk=request.source_id,
            title=raw["title"],
            topic=raw["topic"],
        ).exists()
    if request.feature == AIFeature.TEMPLATE_GENERATION:
        from apps.campaigns.models import MessageTemplate

        return MessageTemplate.objects.filter(
            pk=request.source_id,
            name=raw["name"],
            purpose=raw["purpose"],
        ).exists()
    return True


def _principal_owns_feature_source(*, feature: str, source_id: int, principal: RolePrincipal) -> bool:
    """Apply feature-specific ownership that a branch grant cannot express."""

    if feature != AIFeature.ASSIGNMENT_FEEDBACK or principal.kind != "student":
        return True
    from apps.assignments.models import Submission

    return Submission.objects.filter(pk=source_id, student_id=principal.principal_id).exists()


def validate_exam_generation_parameters(
    *, subject_id: int, exam_type: object, question_count: object, difficulty: object
) -> None:
    """Validate the paid exam-generation intent against live academic catalogues."""

    from apps.academics.models import ExamType, Subject

    if not Subject.objects.filter(pk=subject_id, is_active=True).exists():
        raise NotFoundException(code="ai_source_not_found")
    if (
        not isinstance(exam_type, str)
        or not ExamType.objects.filter(
            slug=exam_type,
            is_active=True,
        ).exists()
    ):
        raise ValidationException(
            "Choose an active exam type.",
            code="validation_error",
            fields={"exam_type": ["Choose an active exam type."]},
        )
    if (
        isinstance(question_count, bool)
        or not isinstance(question_count, int)
        or not 1 <= question_count <= 100
    ):
        raise ValidationException(
            "Invalid question count.",
            code="validation_error",
            fields={"question_count": ["Must be between 1 and 100."]},
        )
    if not isinstance(difficulty, str) or difficulty not in {"easy", "medium", "hard"}:
        raise ValidationException(
            "Invalid difficulty.",
            code="validation_error",
            fields={"difficulty": ["Choose easy, medium, or hard."]},
        )


def _resolved_scope(*, branch_id: int | None, department_id: int | None) -> AISourceAuthorization:
    permission = ""  # assigned by resolve_source_authorization
    if branch_id is None:
        return AISourceAuthorization(
            permission=permission,
            scope_status=AIRequest.ScopeStatus.ORGANIZATION,
            branch_id=None,
            department_id=None,
        )
    return AISourceAuthorization(
        permission=permission,
        scope_status=AIRequest.ScopeStatus.RESOLVED,
        branch_id=int(branch_id),
        department_id=int(department_id) if department_id is not None else None,
    )


def _content_library_scope(library) -> tuple[int | None, int | None]:
    from apps.content.models import ContentLibrary

    if library.visibility == ContentLibrary.Visibility.DEPARTMENT:
        if library.department_id is None:
            raise PermissionException("AI source scope is unavailable.", code="ai_scope_unavailable")
        return library.department.branch_id, library.department_id
    if library.visibility == ContentLibrary.Visibility.COHORT:
        if library.cohort_id is None:
            raise PermissionException("AI source scope is unavailable.", code="ai_scope_unavailable")
        return library.cohort.branch_id, library.cohort.department_id
    # Tenant- and role-visible libraries are global resources.  A branch grant
    # cannot safely mutate/generate into them.
    return None, None


def resolve_source_authorization(
    *,
    feature: str,
    source_app: str,
    source_id: int,
    principal_kind: str | None = None,
) -> AISourceAuthorization:
    """Resolve one reviewed source row and its current exact ownership.

    This runs once before reservation and again in the worker.  Missing, inactive,
    no-longer-draft, or relocated sources fail closed before provider traffic.
    """

    if feature not in _FEATURE_SOURCE_APP or source_app != _FEATURE_SOURCE_APP[feature]:
        raise ValidationException("Unsupported AI source.", code="invalid_ai_source")
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
        raise ValidationException("Unsupported AI source.", code="invalid_ai_source")

    branch_id: int | None
    department_id: int | None
    if feature == AIFeature.ASSIGNMENT_FEEDBACK:
        from apps.assignments.models import Submission

        submission = Submission.objects.select_related("assignment__cohort").filter(pk=source_id).first()
        if submission is None:
            raise NotFoundException(code="ai_source_not_found")
        branch_id = submission.assignment.cohort.branch_id
        department_id = submission.assignment.cohort.department_id
    elif feature == AIFeature.EXAM_GENERATION:
        from apps.academics.models import Subject

        subject = Subject.objects.select_related("department").filter(pk=source_id, is_active=True).first()
        if subject is None:
            raise NotFoundException(code="ai_source_not_found")
        subject_department = subject.department if subject.department_id is not None else None
        if subject.department_id is not None and subject_department is None:
            raise PermissionException("AI source scope is unavailable.", code="ai_scope_unavailable")
        branch_id = subject_department.branch_id if subject_department is not None else None
        department_id = subject.department_id
    elif feature == AIFeature.CONTENT_SUMMARY:
        from apps.content.models import LessonFile

        lesson_file = (
            LessonFile.objects.select_related(
                "folder__library__department",
                "folder__library__cohort",
                "lesson__module__course__library__department",
                "lesson__module__course__library__cohort",
            )
            .filter(pk=source_id, status=LessonFile.Status.CLEAN)
            .first()
        )
        if lesson_file is None:
            raise NotFoundException(code="ai_source_not_found")
        if lesson_file.folder_id is not None:
            folder = lesson_file.folder
            if folder is None:
                raise PermissionException("AI source scope is unavailable.", code="ai_scope_unavailable")
            library = folder.library
        else:
            lesson = lesson_file.lesson
            if lesson is None:
                raise PermissionException("AI source scope is unavailable.", code="ai_scope_unavailable")
            library = lesson.module.course.library
        branch_id, department_id = _content_library_scope(library)
    elif feature == AIFeature.MATERIAL_GENERATION:
        from apps.content.models import LibraryMaterial

        material = (
            LibraryMaterial.objects.select_related("library__department", "library__cohort")
            .filter(pk=source_id, status=LibraryMaterial.Status.DRAFT)
            .first()
        )
        if material is None:
            raise NotFoundException(code="ai_source_not_found")
        branch_id, department_id = _content_library_scope(material.library)
    elif feature == AIFeature.PLACEMENT_GENERATION:
        from apps.placement.models import PlacementTest

        placement_test = (
            PlacementTest.objects.select_related("subject__department")
            .filter(pk=source_id, status=PlacementTest.Status.DRAFT)
            .first()
        )
        if placement_test is None:
            raise NotFoundException(code="ai_source_not_found")
        branch_id = placement_test.branch_id
        placement_subject = placement_test.subject
        placement_department = (
            placement_subject.department
            if placement_subject is not None and placement_subject.department_id is not None
            else None
        )
        if placement_department is not None and placement_department.branch_id != placement_test.branch_id:
            raise PermissionException(
                "AI source scope is unavailable.",
                code="ai_scope_unavailable",
            )
        department_id = placement_department.pk if placement_department is not None else None
    elif feature == AIFeature.WRITING_MARKING:
        from apps.placement.models import PlacementAttempt, PlacementQuestion

        attempt = (
            PlacementAttempt.objects.select_related("test__subject__department")
            .filter(
                pk=source_id,
                status=PlacementAttempt.Status.GRADED,
                answers__question__question_type=PlacementQuestion.QuestionType.WRITING,
            )
            .distinct()
            .first()
        )
        if attempt is None:
            raise NotFoundException(code="ai_source_not_found")
        branch_id = attempt.test.branch_id
        attempt_subject = attempt.test.subject
        attempt_department = (
            attempt_subject.department
            if attempt_subject is not None and attempt_subject.department_id is not None
            else None
        )
        if attempt_department is not None and attempt_department.branch_id != attempt.test.branch_id:
            raise PermissionException(
                "AI source scope is unavailable.",
                code="ai_scope_unavailable",
            )
        department_id = attempt_department.pk if attempt_department is not None else None
    elif feature == AIFeature.FORM_ANALYSIS:
        from apps.forms.models import Form

        form = Form.objects.filter(pk=source_id, responses__isnull=False).distinct().first()
        if form is None:
            raise NotFoundException(code="ai_source_not_found")
        branch_id = form.branch_id
        department_id = None
    elif feature == AIFeature.TEMPLATE_GENERATION:
        from apps.campaigns.models import MessageTemplate

        if not MessageTemplate.objects.filter(pk=source_id, is_active=True).exists():
            raise NotFoundException(code="ai_source_not_found")
        branch_id = None
        department_id = None
    else:  # pragma: no cover - guarded by the catalogue above
        raise ValidationException("Unsupported AI source.", code="invalid_ai_source")

    resolved = _resolved_scope(branch_id=branch_id, department_id=department_id)
    permission = _FEATURE_PERMISSION[feature]
    if feature == AIFeature.ASSIGNMENT_FEEDBACK:
        if principal_kind in {"staff", "teacher"}:
            permission = "assignments:write"
        elif principal_kind not in {None, "student"}:
            raise PermissionException(
                "AI requester identity is unavailable.", code="ai_principal_unavailable"
            )
    return AISourceAuthorization(
        permission=permission,
        scope_status=resolved.scope_status,
        branch_id=resolved.branch_id,
        department_id=resolved.department_id,
    )


def resolve_request_principal(
    *,
    requested_by,
    requested_principal: RolePrincipal | None,
    feature: str,
    source_id: int,
) -> RolePrincipal:
    """Return an exact active principal; never infer an ambiguous bridge role."""

    user_id = getattr(requested_by, "pk", None)
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise PermissionException("AI requester identity is unavailable.", code="ai_principal_unavailable")
    if requested_principal is not None:
        if requested_principal.user_id != user_id:
            raise PermissionException(
                "AI requester identity is unavailable.", code="ai_principal_unavailable"
            )
        principal = validate_role_principal(
            kind=requested_principal.kind,
            principal_id=requested_principal.principal_id,
            user_id=user_id,
            field="principal",
        )
        if not _principal_owns_feature_source(
            feature=feature,
            source_id=source_id,
            principal=principal,
        ):
            raise PermissionException(
                "AI source is outside the active account scope.",
                code="ai_scope_unavailable",
            )
        return principal

    # Automatic submission feedback has a role-native owner on the source row.
    # This is exact even when the student's compatibility User also backs another
    # profile.  Every other compatibility path must be unambiguous.
    if feature == AIFeature.ASSIGNMENT_FEEDBACK:
        from apps.assignments.models import Submission

        student_id = (
            Submission.objects.filter(pk=source_id, student__user_id=user_id)
            .values_list("student_id", flat=True)
            .first()
        )
        if student_id is not None:
            return validate_role_principal(
                kind="student",
                principal_id=int(student_id),
                user_id=user_id,
                field="principal",
            )

    try:
        principal = resolve_unambiguous_user_principal(
            user_id,
            field="principal",
            message="The active role account could not be identified.",
        )
    except ValidationException as exc:
        raise PermissionException(
            "AI requester identity is unavailable.", code="ai_principal_unavailable"
        ) from exc
    if not _principal_owns_feature_source(
        feature=feature,
        source_id=source_id,
        principal=principal,
    ):
        raise PermissionException(
            "AI source is outside the active account scope.",
            code="ai_scope_unavailable",
        )
    return principal


def principal_authorizes_source(
    *,
    user,
    principal: RolePrincipal,
    source: AISourceAuthorization,
) -> bool:
    if not bool(getattr(user, "is_active", False)) or principal.user_id != getattr(user, "pk", None):
        return False
    try:
        roles, _memberships = get_user_authorization_context(
            user,
            principal_kind=principal.kind,
            principal_id=principal.principal_id,
            principal_validated=False,
        )
    except (TypeError, ValueError):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if source.scope_status == AIRequest.ScopeStatus.ORGANIZATION:
        return permission_membership_is_unscoped(roles=roles, permission=source.permission)
    if source.scope_status != AIRequest.ScopeStatus.RESOLVED or source.branch_id is None:
        return False
    for grant in permission_membership_scopes(roles=roles, permission=source.permission):
        if grant.is_organization_wide:
            return True
        if grant.branch_id != source.branch_id:
            continue
        if grant.department_id is None or grant.department_id == source.department_id:
            return True
    return False


def assert_principal_authorizes_source(
    *, user, principal: RolePrincipal, source: AISourceAuthorization
) -> None:
    if not principal_authorizes_source(user=user, principal=principal, source=source):
        raise PermissionException(
            "AI source is outside the active account scope.", code="ai_scope_unavailable"
        )


def request_source_matches_live_state(request: AIRequest) -> bool:
    """Re-resolve source ownership and compare it to the immutable snapshot."""

    principal = request_principal(request)
    if principal is None or not _principal_owns_feature_source(
        feature=request.feature,
        source_id=request.source_id,
        principal=principal,
    ):
        return False
    try:
        live = resolve_source_authorization(
            feature=request.feature,
            source_app=request.source_app,
            source_id=request.source_id,
            principal_kind=request.requested_principal_kind,
        )
    except (NotFoundException, PermissionException, ValidationException):
        return False
    return (
        live.permission == request.authorization_permission
        and live.scope_status == request.scope_status
        and live.branch_id == request.branch_at_request_id
        and live.department_id == request.department_at_request_id
    )


def request_principal(request: AIRequest) -> RolePrincipal | None:
    if (
        request.attribution_status != AIRequest.AttributionStatus.RESOLVED
        or request.requested_by_id is None
        or not request.requested_principal_kind
        or request.requested_principal_id is None
    ):
        return None
    return RolePrincipal(
        kind=request.requested_principal_kind,
        principal_id=int(request.requested_principal_id),
        user_id=int(request.requested_by_id),
    )


def request_is_live_authorized(request: AIRequest) -> bool:
    principal = request_principal(request)
    if principal is None or request.requested_by is None or not request_source_matches_live_state(request):
        return False
    source = AISourceAuthorization(
        permission=request.authorization_permission,
        scope_status=request.scope_status,
        branch_id=request.branch_at_request_id,
        department_id=request.department_at_request_id,
    )
    return principal_authorizes_source(user=request.requested_by, principal=principal, source=source)


def scope_query_for_permission(*, roles, permission: str) -> Q:
    """Visibility predicate for immutable AI request scope snapshots."""

    if permission_membership_is_unscoped(roles=roles, permission=permission):
        return Q(pk__isnull=False)
    visible = Q(pk__in=[])
    for grant in permission_membership_scopes(roles=roles, permission=permission):
        if grant.department_id is None:
            visible |= Q(
                scope_status=AIRequest.ScopeStatus.RESOLVED,
                branch_at_request_id=grant.branch_id,
            )
        else:
            visible |= Q(
                scope_status=AIRequest.ScopeStatus.RESOLVED,
                branch_at_request_id=grant.branch_id,
                department_at_request_id=grant.department_id,
            )
    return visible
