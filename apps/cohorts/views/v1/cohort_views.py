"""Cohort endpoints — plain Django views over the layered architecture.

Collection (GET list / POST create) and detail (GET / PUT / PATCH / DELETE) dispatch
on method so the required perm tracks it (cohorts:read for reads, cohorts:write for
writes); the custom actions (enroll / move-student / members / unarchive) mirror the
old viewset's per-action perms. Branch scoping matches the old ObjectScopedPermission:
lists are scoped, and every detail/action asserts the cohort is in the caller's branches.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.cohorts.dto.cohort_dto import (
    CohortCreateDTO,
    CohortEnrollDTO,
    CohortMoveDTO,
    CohortRemoveDTO,
    CohortTeacherDTO,
)
from apps.cohorts.interfaces.cohort_service import ICohortService
from apps.cohorts.presenters import cohort_teacher_to_dict, cohort_to_dict, membership_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.responses import created, error, no_content, paginated, success
from core.scoping import assert_branch_id_in_scope, assert_in_branch_scope, scope_to_branches

_RESOURCE = "cohorts"
_FILTERS = ("branch", "department", "is_archived")
_SEARCH = ("name", "level")
_ORDERING = ("start_date", "created_at", "name")


def _service() -> ICohortService:
    return container.resolve(ICohortService)  # type: ignore[type-abstract]


def _get_in_scope(request: HttpRequest, pk: int):
    """Fetch the cohort or 404, then assert it is in the caller's branches (403 else)."""
    cohort = _service().get(pk)
    if cohort is None:
        raise NotFoundException(code="not_found")
    assert_in_branch_scope(request, cohort)
    return cohort


@csrf_exempt
@require_auth
def cohorts_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        return _list(request)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def cohort_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    cohort = _get_in_scope(request, pk)

    if read:
        return success(cohort_to_dict(cohort))
    if request.method in ("PUT", "PATCH"):
        # PUT and PATCH are both partial here (apply only the fields present) — the
        # deliberate, mobile-friendly convention used across the off-DRF migration.
        # Assert archived BEFORE parsing the body so an archived cohort always answers
        # `cohort_archived`, never a field-validation error (parity with the old view).
        if cohort.is_archived:
            raise ValidationException("Cohort is archived.", code="cohort_archived")
        changes = _changes(read_json(request))
        if "branch" in changes:  # reassignment must land in a branch the caller can reach
            assert_branch_id_in_scope(request, changes["branch"])
        updated = _service().update(cohort, changes)
        return success(cohort_to_dict(updated))
    if request.method == "DELETE":
        _service().delete(cohort)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def cohort_enroll_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    cohort = _get_in_scope(request, pk)
    body = read_json(request)
    dto = CohortEnrollDTO(
        student_id=int_field(body, "student", required=True),  # type: ignore[arg-type]
        start_date=_date(body, "start_date"),
    )
    return created(membership_to_dict(_service().enroll(cohort, dto)))


@csrf_exempt
@require_auth
def cohort_move_student_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    cohort = _get_in_scope(request, pk)
    body = read_json(request)
    dto = CohortMoveDTO(
        student_id=int_field(body, "student", required=True),  # type: ignore[arg-type]
        reason=str_field(body, "reason"),
    )
    result = _service().move(cohort, dto, actor=request.user)
    return success(
        {
            "membership": membership_to_dict(result["membership"]),
            "over_capacity": result["over_capacity"],
        }
    )


@csrf_exempt
@require_auth
def cohort_remove_student_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove a student FROM this cohort without moving them elsewhere (F2 "remove from
    group" — they stay enrolled in the centre, just groupless for this course)."""
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    cohort = _get_in_scope(request, pk)
    body = read_json(request)
    dto = CohortRemoveDTO(
        student_id=int_field(body, "student", required=True),  # type: ignore[arg-type]
        reason=str_field(body, "reason"),
    )
    return success(membership_to_dict(_service().remove_member(cohort, dto, actor=request.user)))


@csrf_exempt
@require_auth
def cohort_members_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    cohort = _get_in_scope(request, pk)
    return success([membership_to_dict(m) for m in _service().members(cohort)])


@csrf_exempt
@require_auth
def cohort_teachers_view(request: HttpRequest, pk: int) -> HttpResponse:
    """The cohort's co-teacher/assistant roster (F4): GET lists it, POST assigns."""
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        cohort = _get_in_scope(request, pk)
        return success([cohort_teacher_to_dict(ct) for ct in _service().co_teachers(cohort)])
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        cohort = _get_in_scope(request, pk)
        body = read_json(request)
        dto = CohortTeacherDTO(
            teacher_id=int_field(body, "teacher", required=True),  # type: ignore[arg-type]
            role=str_field(body, "role", default="co_teacher"),
        )
        ct, was_created = _service().assign_teacher(cohort, dto)
        payload = cohort_teacher_to_dict(ct)
        return created(payload) if was_created else success(payload)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def cohort_teacher_detail_view(request: HttpRequest, pk: int, teacher_id: int) -> HttpResponse:
    """Unassign a co-teacher/assistant from the cohort (F4)."""
    if request.method != "DELETE":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    cohort = _get_in_scope(request, pk)
    _service().remove_teacher(cohort, teacher_id)
    return no_content()


@csrf_exempt
@require_auth
def cohort_unarchive_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    cohort = _get_in_scope(request, pk)
    return success(cohort_to_dict(_service().unarchive(cohort)))


# --- helpers ---------------------------------------------------------------
def _list(request: HttpRequest) -> HttpResponse:
    qs = scope_to_branches(request, _service().list())
    qs = apply_filters(
        request,
        qs,
        filter_fields=_FILTERS,
        search_fields=_SEARCH,
        ordering_fields=_ORDERING,
        default_ordering="-created_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([cohort_to_dict(c) for c in items], total=total, page=page, page_size=size)


def _create(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    branch_id = int_field(body, "branch", required=True)
    assert_branch_id_in_scope(request, branch_id)  # create-scope (no object yet)
    dto = CohortCreateDTO(
        name=str_field(body, "name"),
        branch_id=branch_id,  # type: ignore[arg-type]
        start_date=_date(body, "start_date", required=True),  # type: ignore[arg-type]
        end_date=_date(body, "end_date", required=True),  # type: ignore[arg-type]
        department_id=int_field(body, "department"),
        level=str_field(body, "level"),
        capacity=int_field(body, "capacity"),
        primary_teacher_id=int_field(body, "primary_teacher"),
        default_room_id=int_field(body, "default_room"),
        is_archived=bool_field(body, "is_archived"),
    )
    if not dto.name:
        raise ValidationException(
            "Name is required.", code="validation_error", fields={"name": ["This field is required."]}
        )
    return created(cohort_to_dict(_service().create(dto)))


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    """The provided updatable fields only (PATCH-correct: absent vs null differ)."""
    changes: dict[str, Any] = {}
    if "name" in body:
        changes["name"] = str_field(body, "name")
    if "branch" in body:
        changes["branch"] = int_field(body, "branch", required=True)
    if "department" in body:
        changes["department"] = int_field(body, "department")
    if "level" in body:
        changes["level"] = str_field(body, "level")
    if "start_date" in body:
        changes["start_date"] = _date(body, "start_date", required=True)
    if "end_date" in body:
        changes["end_date"] = _date(body, "end_date", required=True)
    if "capacity" in body:
        changes["capacity"] = int_field(body, "capacity")
    if "primary_teacher" in body:
        changes["primary_teacher"] = int_field(body, "primary_teacher")
    if "default_room" in body:
        changes["default_room"] = int_field(body, "default_room")
    if "is_archived" in body:
        changes["is_archived"] = bool_field(body, "is_archived")
    return changes


def _date(body: dict[str, Any], name: str, *, required: bool = False) -> date | None:
    raw = body.get(name)
    if raw in (None, ""):
        if required:
            raise ValidationException(
                "Date is required.",
                code="validation_error",
                fields={name: ["This field is required."]},
            )
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise ValidationException(
            "Invalid date.", code="validation_error", fields={name: ["Must be an ISO date."]}
        ) from None
