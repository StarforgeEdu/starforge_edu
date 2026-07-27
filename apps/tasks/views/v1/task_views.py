"""Task + role-hierarchy endpoints — plain Django views over the layered architecture.

RoleGrade is the per-centre hierarchy (read = tasks:read; edit = tasks:assign_any).
Tasks (tasks:write to create/assign; the assignee, tasks:read, transitions their own
work). Reads are ROW-scoped: a director sees all; everyone else sees tasks assigned to
them / created by them / in their department(s), plus (with tasks:write) their branch(es).
"""

from __future__ import annotations

from typing import Any, NamedTuple

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.tasks.dto.task_dto import AssignTaskDTO, CreateTaskDTO, RoleGradeDTO
from apps.tasks.interfaces.services import IRoleGradeService, ITaskService
from apps.tasks.models import Task
from apps.tasks.presenters import role_grade_to_dict, task_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import (
    Role,
    _request_overrides,
    get_role_memberships,
    get_user_roles,
    has_permission_code,
)
from core.responses import created, error, no_content, paginated, success

_RESOURCE = "tasks"


def _task_service() -> ITaskService:
    return container.resolve(ITaskService)  # type: ignore[type-abstract]


def _grade_service() -> IRoleGradeService:
    return container.resolve(IRoleGradeService)  # type: ignore[type-abstract]


class _Scope(NamedTuple):
    is_superuser: bool
    is_unscoped: bool
    has_write: bool
    roles: set[str]
    branch_ids: set[int]
    dept_ids: set[int]


def _scope(request: HttpRequest) -> _Scope:
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    is_superuser = getattr(req.user, "is_superuser", False)
    memberships = get_role_memberships(req)
    return _Scope(
        is_superuser=is_superuser,
        is_unscoped=is_superuser or Role.DIRECTOR in roles,
        has_write=has_permission_code(roles, f"{_RESOURCE}:write", _request_overrides(req)),
        roles=roles,
        branch_ids={m.branch_id for m in memberships if m.branch_id},
        dept_ids={m.department_id for m in memberships if m.department_id},
    )


# --- role grades -----------------------------------------------------------
@csrf_exempt
@require_auth
def role_grades_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        # No default_ordering: keep the model's compound Meta.ordering ("-level", "role")
        # when no ?ordering is given, so equal-level grades keep their deterministic
        # role tiebreak (a single-key default_ordering would drop it).
        qs = apply_filters(request, _grade_service().list(), ordering_fields=("level", "role"))
        items, total, page, size = paginate(request, qs)
        return paginated([role_grade_to_dict(g) for g in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:assign_any")
        return created(role_grade_to_dict(_grade_service().create(_grade_dto(read_json(request)))))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def role_grade_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:assign_any")
    grade = _grade_service().get(pk)
    if grade is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(role_grade_to_dict(grade))
    if request.method in ("PUT", "PATCH"):
        return success(role_grade_to_dict(_grade_service().update(grade, _grade_changes(read_json(request)))))
    if request.method == "DELETE":
        _grade_service().delete(grade)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _grade_dto(body: dict[str, Any]) -> RoleGradeDTO:
    role = str_field(body, "role", max_length=32).strip()
    if not role:
        raise ValidationException(
            "Role is required.", code="validation_error", fields={"role": ["This field is required."]}
        )
    return RoleGradeDTO(
        role=role, level=_level(body, required=True), label=str_field(body, "label", max_length=64)
    )


def _grade_changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "role" in body:
        role = str_field(body, "role", max_length=32).strip()
        if not role:
            raise ValidationException(
                "Role may not be blank.", code="validation_error", fields={"role": ["May not be blank."]}
            )
        changes["role"] = role
    if "level" in body:
        changes["level"] = _level(body, required=True)
    if "label" in body:
        changes["label"] = str_field(body, "label", max_length=64)
    return changes


def _level(body: dict[str, Any], *, required: bool) -> int:
    value = int_field(body, "level", required=required)
    if value is None:  # only reachable when required=False
        return 0
    if value < 0:  # PositiveIntegerField — a negative level is a clean 400, not a DB error
        raise ValidationException(
            "Level must be non-negative.", code="validation_error", fields={"level": ["Must be >= 0."]}
        )
    return value


# --- tasks -----------------------------------------------------------------
@csrf_exempt
@require_auth
def tasks_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        s = _scope(request)
        qs = _task_service().scoped_list(
            user=request.user,
            is_unscoped=s.is_unscoped,
            has_write=s.has_write,
            branch_ids=s.branch_ids,
            dept_ids=s.dept_ids,
        )
        qs = apply_filters(
            request,
            qs,
            filter_fields=("status", "priority", "assignee", "department", "branch"),
            search_fields=("title",),
            ordering_fields=("created_at", "due_at", "priority"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([task_to_dict(t) for t in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create_task(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def tasks_mine_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    qs = _task_service().mine(request.user)
    items, total, page, size = paginate(request, qs)
    return paginated([task_to_dict(t) for t in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def task_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    return success(task_to_dict(_get_visible(request, pk)))


@csrf_exempt
@require_auth
def task_assign_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    s = _scope(request)
    task = _get_visible(request, pk)
    body = read_json(request)
    dto = AssignTaskDTO(
        assignee_provided="assignee" in body,
        assignee_id=int_field(body, "assignee"),
        department_provided="department" in body,
        department_id=int_field(body, "department"),
    )
    result = _task_service().assign(
        task, dto, actor=request.user, actor_roles=s.roles, is_unscoped=s.is_unscoped, branch_ids=s.branch_ids
    )
    return success(task_to_dict(result))


@csrf_exempt
@require_auth
def task_transition_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    task = _get_visible(request, pk)
    to_status = str_field(read_json(request), "status")
    return success(task_to_dict(_task_service().transition(task, to_status=to_status, actor=request.user)))


@csrf_exempt
@require_auth
def task_auto_assign_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    s = _scope(request)
    body = read_json(request)
    mode = str_field(body, "mode", default="fair")
    if mode not in ("fair", "free"):
        raise ValidationException(
            "Invalid mode.", code="validation_error", fields={"mode": ['Must be "fair" or "free".']}
        )
    result = _task_service().auto_assign(
        task_ids=_task_ids(body),
        department_id=int_field(body, "department", required=True),  # type: ignore[arg-type]
        actor=request.user,
        actor_roles=s.roles,
        mode=mode,
        is_unscoped=s.is_unscoped,
        branch_ids=s.branch_ids,
    )
    return success(result)


# --- helpers ---------------------------------------------------------------
def _get_visible(request: HttpRequest, pk: int) -> Task:
    s = _scope(request)
    task = _task_service().get_visible(
        user=request.user,
        is_unscoped=s.is_unscoped,
        has_write=s.has_write,
        branch_ids=s.branch_ids,
        dept_ids=s.dept_ids,
        pk=pk,
    )
    if task is None:
        raise NotFoundException(code="not_found")  # not in the caller's scope -> 404, no leak
    return task


def _create_task(request: HttpRequest) -> HttpResponse:
    s = _scope(request)
    body = read_json(request)
    title = str_field(body, "title", max_length=200).strip()
    if not title:
        raise ValidationException(
            "Title is required.", code="validation_error", fields={"title": ["This field is required."]}
        )
    priority = str_field(body, "priority", default=Task.Priority.NORMAL)
    if priority not in Task.Priority.values:
        raise ValidationException(
            "Invalid priority.",
            code="validation_error",
            fields={"priority": [f"Must be one of {', '.join(Task.Priority.values)}."]},
        )
    dto = CreateTaskDTO(
        title=title,
        description=str_field(body, "description"),
        priority=priority,
        assignee_id=int_field(body, "assignee"),
        department_id=int_field(body, "department"),
        branch_id=int_field(body, "branch"),
        due_at=_optional_datetime(body, "due_at"),
    )
    task = _task_service().create(
        dto,
        creator=request.user,
        creator_roles=s.roles,
        is_superuser=s.is_superuser,
        is_unscoped=s.is_unscoped,
        branch_ids=s.branch_ids,
    )
    return created(task_to_dict(task))


def _optional_datetime(body: dict[str, Any], name: str):
    raw = body.get(name)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ValidationException(
            "Invalid datetime.", code="validation_error", fields={name: ["Must be an ISO 8601 datetime."]}
        )
    try:
        # parse_datetime RAISES ValueError on a well-formed-but-invalid value (2026-02-30)
        dt = parse_datetime(raw)
    except ValueError:
        dt = None
    if dt is None:
        raise ValidationException(
            "Invalid datetime.", code="validation_error", fields={name: ["Must be an ISO 8601 datetime."]}
        )
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _task_ids(body: dict[str, Any]) -> list[int]:
    raw = body.get("task_ids")
    if not isinstance(raw, list) or not raw:  # allow_empty=False
        raise ValidationException(
            "task_ids is required.",
            code="validation_error",
            fields={"task_ids": ["A non-empty list of ids is required."]},
        )
    if len(raw) > 500:  # old serializer max_length=500
        raise ValidationException(
            "Too many task ids.", code="validation_error", fields={"task_ids": ["At most 500 ids."]}
        )
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:  # IntegerField(min_value=1)
            raise ValidationException(
                "Invalid task id.",
                code="validation_error",
                fields={"task_ids": ["Each item must be a positive integer id."]},
            )
        out.append(item)
    return out
