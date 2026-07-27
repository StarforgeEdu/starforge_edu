"""Assignment + submission endpoints — plain Django views over the layered architecture.

Role-scoped reads (a student sees published assignments in their own cohorts + their own
submissions; a teacher their taught cohorts; a director/HoD all) — out-of-scope rows 404,
never a 403 that leaks existence. Writes are scoped too: a teacher may only author into a
cohort they teach. Submissions are created via /assignments/{id}/submissions/ (submit),
graded via /assignments/submissions/{id}/grade/.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.assignments.dto.assignment_dto import CreateAssignmentDTO
from apps.assignments.interfaces.services import IAssignmentService, ISubmissionService
from apps.assignments.presenters import assignment_to_dict, grade_to_dict, submission_to_dict
from apps.students.models import StudentProfile
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import decimal_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, no_content, paginated, success

_RESOURCE = "assignments"


def _assignment_service() -> IAssignmentService:
    return container.resolve(IAssignmentService)  # type: ignore[type-abstract]


def _submission_service() -> ISubmissionService:
    return container.resolve(ISubmissionService)  # type: ignore[type-abstract]


def _roles(request: HttpRequest) -> set[str]:
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    return get_user_roles(req)


def _get_assignment(request: HttpRequest, pk: int):
    assignment = _assignment_service().get_visible(user=request.user, roles=_roles(request), pk=pk)
    if assignment is None:
        raise NotFoundException(code="not_found")  # scoped out -> 404, no existence leak
    return assignment


def _get_submission(request: HttpRequest, pk: int):
    submission = _submission_service().get_visible(user=request.user, roles=_roles(request), pk=pk)
    if submission is None:
        raise NotFoundException(code="not_found")
    return submission


# --- assignments -----------------------------------------------------------
@csrf_exempt
@require_auth
def assignments_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = _assignment_service().scoped_list(user=request.user, roles=_roles(request))
        qs = apply_filters(
            request,
            qs,
            filter_fields=("cohort", "status"),
            ordering_fields=("due_at", "created_at"),
            default_ordering="-due_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([assignment_to_dict(a) for a in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create_assignment(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def assignment_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    assignment = _get_assignment(request, pk)
    if read:
        return success(assignment_to_dict(assignment))
    if request.method in ("PUT", "PATCH"):
        result = _assignment_service().update(
            assignment, _update_changes(request), user=request.user, roles=_roles(request)
        )
        return success(assignment_to_dict(result))
    if request.method == "DELETE":
        _assignment_service().delete(assignment)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def assignment_publish_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    assignment = _get_assignment(request, pk)
    return success(assignment_to_dict(_assignment_service().publish(assignment, actor=request.user)))


@csrf_exempt
@require_auth
def assignment_submissions_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:write")  # teacher list
        assignment = _get_assignment(request, pk)
        rows = _assignment_service().submissions_of(assignment, user=request.user, roles=_roles(request))
        return success([submission_to_dict(s) for s in rows])
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:submit")  # student submit
        assignment = _get_assignment(request, pk)
        actor: Any = request.user  # a real User post-@require_auth (typed User|AnonymousUser)
        student = StudentProfile.objects.filter(user=actor).first()
        if student is None:
            raise PermissionException("Only an enrolled student may submit.", code="not_a_student")
        body = read_json(request)
        submission = _assignment_service().submit(
            assignment,
            student=student,
            text=str_field(body, "text", max_length=20_000),
            attachment_keys=_attachment_keys(body),
        )
        return created(submission_to_dict(submission))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def assignment_upload_url_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write", f"{_RESOURCE}:submit")  # either may presign
    body = read_json(request)
    filename = str_field(body, "filename", max_length=255)
    if not filename:
        raise ValidationException(
            "filename is required.", code="validation_error", fields={"filename": ["Required."]}
        )
    size_bytes = int_field(body, "size_bytes", required=True)
    if size_bytes is None or size_bytes < 1:  # IntegerField(min_value=1)
        raise ValidationException(
            "size_bytes must be a positive integer.",
            code="validation_error",
            fields={"size_bytes": ["Must be >= 1."]},
        )
    result = _assignment_service().upload_url(
        filename=filename,
        content_type=str_field(body, "content_type", default="application/octet-stream", max_length=127),
        size_bytes=size_bytes,
    )
    return success(result)


# --- submissions -----------------------------------------------------------
@csrf_exempt
@require_auth
def submissions_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = _submission_service().scoped_list(user=request.user, roles=_roles(request))
        items, total, page, size = paginate(request, qs)
        return paginated([submission_to_dict(s) for s in items], total=total, page=page, page_size=size)
    # No generic create — submissions are made via /assignments/{id}/submissions/.
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def submission_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    return success(submission_to_dict(_get_submission(request, pk)))


@csrf_exempt
@require_auth
def submission_grade_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    submission = _get_submission(request, pk)
    body = read_json(request)
    score = decimal_field(body, "score", max_digits=6)
    if score is None:
        raise ValidationException(
            "score is required.", code="validation_error", fields={"score": ["This field is required."]}
        )
    rubric_scores = body.get("rubric_scores", [])
    if not isinstance(rubric_scores, list):
        raise ValidationException(
            "rubric_scores must be a list.",
            code="validation_error",
            fields={"rubric_scores": ["Must be a list."]},
        )
    for rs in rubric_scores:
        # grade_submission does rs.get("criterion") and `criterion in valid_criteria` —
        # a non-dict item (AttributeError) or an unhashable criterion (TypeError) would
        # 500. Require each item is an object with a string criterion -> clean 400.
        if not isinstance(rs, dict) or not isinstance(rs.get("criterion"), str):
            raise ValidationException(
                "Each rubric score must be an object with a string criterion.",
                code="validation_error",
                fields={"rubric_scores": ["Each item must be an object with a string 'criterion'."]},
            )
    grade = _submission_service().grade(
        submission,
        score=score,
        rubric_scores=rubric_scores,
        feedback=str_field(body, "feedback"),
        actor=request.user,
    )
    return success(grade_to_dict(grade))


@csrf_exempt
@require_auth
def submission_ai_feedback_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    submission = _get_submission(request, pk)
    _submission_service().request_ai_feedback(submission, requested_by=request.user)
    return success({"status": "queued"}, status=202)


# --- helpers ---------------------------------------------------------------
def _create_assignment(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    title = str_field(body, "title", max_length=200).strip()
    if not title:
        raise ValidationException(
            "Title is required.", code="validation_error", fields={"title": ["This field is required."]}
        )
    cohort_id = int_field(body, "cohort", required=True)
    dto = CreateAssignmentDTO(
        cohort_id=cohort_id,  # type: ignore[arg-type]
        title=title,
        due_at=_required_datetime(body, "due_at"),
        description=str_field(body, "description"),
        attachments=_json_list(body, "attachments"),
        rubric=body.get("rubric", []),
        max_score=decimal_field(body, "max_score", max_digits=6),
        max_resubmits=int_field(body, "max_resubmits"),
    )
    assignment = _assignment_service().create(
        dto, creator=request.user, user=request.user, roles=_roles(request)
    )
    return created(assignment_to_dict(assignment))


def _update_changes(request: HttpRequest) -> dict[str, Any]:
    body = read_json(request)
    changes: dict[str, Any] = {}
    if "cohort" in body:
        changes["cohort"] = int_field(body, "cohort", required=True)
    if "title" in body:
        title = str_field(body, "title", max_length=200).strip()
        if not title:
            raise ValidationException(
                "Title may not be blank.", code="validation_error", fields={"title": ["May not be blank."]}
            )
        changes["title"] = title
    if "description" in body:
        changes["description"] = str_field(body, "description")
    if "due_at" in body:
        changes["due_at"] = _required_datetime(body, "due_at")
    if "attachments" in body:
        changes["attachments"] = _json_list(body, "attachments")
    if "rubric" in body:
        changes["rubric"] = body["rubric"]
    if "max_score" in body:
        max_score = decimal_field(body, "max_score", max_digits=6)
        if max_score is None:  # explicit null/"" — the column is NOT NULL (old serializer 400'd)
            raise ValidationException(
                "max_score may not be null.",
                code="validation_error",
                fields={"max_score": ["May not be null."]},
            )
        changes["max_score"] = max_score
    if "max_resubmits" in body:
        changes["max_resubmits"] = int_field(body, "max_resubmits")
    return changes


def _json_list(body: dict[str, Any], name: str) -> list:
    value = body.get(name, [])
    if not isinstance(value, list):
        raise ValidationException(
            f"{name} must be a list.", code="validation_error", fields={name: ["Must be a list."]}
        )
    return value


def _attachment_keys(body: dict[str, Any]) -> list:
    keys = body.get("attachment_keys", [])
    if not isinstance(keys, list):
        raise ValidationException(
            "attachment_keys must be a list.",
            code="validation_error",
            fields={"attachment_keys": ["Must be a list of keys."]},
        )
    if len(keys) > 20:  # old serializer max_length=20
        raise ValidationException(
            "Too many attachment keys.",
            code="validation_error",
            fields={"attachment_keys": ["At most 20 keys."]},
        )
    for k in keys:
        if not isinstance(k, str) or len(k) > 1024:
            raise ValidationException(
                "Invalid attachment key.",
                code="validation_error",
                fields={"attachment_keys": ["Each key must be a string of at most 1024 chars."]},
            )
    return keys


def _required_datetime(body: dict[str, Any], name: str):
    raw = body.get(name)
    if not raw or not isinstance(raw, str):
        raise ValidationException(
            f"{name} is required.", code="validation_error", fields={name: ["Required (ISO 8601)."]}
        )
    try:
        # parse_datetime RAISES ValueError on a regex-valid-but-impossible value.
        dt = parse_datetime(raw)
    except ValueError:
        dt = None
    if dt is None:
        raise ValidationException(
            "Invalid datetime.", code="validation_error", fields={name: ["Must be an ISO 8601 datetime."]}
        )
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt
