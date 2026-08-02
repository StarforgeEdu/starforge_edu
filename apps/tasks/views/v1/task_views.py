"""Task + role-hierarchy endpoints — plain Django views over the layered architecture.

RoleGrade is the per-centre hierarchy (read = tasks:read; edit = tasks:assign_any).
Tasks (tasks:write to create/assign; the assignee, tasks:read, transitions their own
work). Reads are ROW-scoped: a director sees all; everyone else sees tasks assigned to
them / created by them / in their department(s), plus (with tasks:write) their branch(es).
"""

from __future__ import annotations

from typing import Any, NamedTuple, cast

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.tasks.dto.task_dto import AssignTaskDTO, CreateTaskDTO, RoleGradeDTO
from apps.tasks.interfaces.services import IRoleGradeService, ITaskService
from apps.tasks.models import Task
from apps.tasks.openapi_contracts import (
    ROLE_GRADE_DETAIL_CONTRACTS,
    ROLE_GRADES_COLLECTION_CONTRACTS,
    TASK_ASSIGN_CONTRACT,
    TASK_AUTO_ASSIGN_CONTRACT,
    TASK_DETAIL_CONTRACTS,
    TASK_TRANSITION_CONTRACT,
    TASKS_COLLECTION_CONTRACTS,
    TASKS_MINE_CONTRACTS,
)
from apps.tasks.presenters import role_grade_to_dict, task_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import int_field, read_json, str_field
from core.listing import apply_filters, paginate, positive_int_filter, validate_pagination_filters
from core.openapi_contracts import openapi_contract
from core.permissions import MembershipGrantScope, get_user_roles
from core.responses import created, error, no_content, paginated, success
from core.role_principals import (
    STAFF_PRINCIPAL_KINDS,
    RolePrincipal,
    request_role_principal,
)
from core.scoping import (
    is_permission_unscoped,
    permission_membership_scopes,
)

_RESOURCE = "tasks"
MAX_TASK_DESCRIPTION_CHARS = 20_000
_CREATE_FIELDS = frozenset(
    {
        "title",
        "description",
        "priority",
        "assignee",
        "assignee_principal",
        "department",
        "branch",
        "due_at",
    }
)


def _task_service() -> ITaskService:
    return container.resolve(ITaskService)  # type: ignore[type-abstract]


def _grade_service() -> IRoleGradeService:
    return container.resolve(IRoleGradeService)  # type: ignore[type-abstract]


class _Scope(NamedTuple):
    is_superuser: bool
    is_unscoped: bool
    principal: RolePrincipal
    grants: tuple[MembershipGrantScope, ...]
    branch_ids: set[int]
    dept_ids: set[int]

    def allows(self, task: Task) -> bool:
        if self.is_unscoped:
            return True
        if task.branch_id is None:
            return False
        return any(
            grant.branch_id == task.branch_id
            and (grant.department_id is None or grant.department_id == task.department_id)
            for grant in self.grants
        )


def _request_principal(request: HttpRequest) -> RolePrincipal:
    cached = getattr(request, "_tasks_role_principal", None)
    if isinstance(cached, RolePrincipal):
        return cached
    principal = request_role_principal(
        request,
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        error_code="tasks_principal_unavailable",
    )
    request._tasks_role_principal = principal  # type: ignore[attr-defined]
    return principal


def _scope(request: HttpRequest, *, permission: str = f"{_RESOURCE}:read") -> _Scope:
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    is_superuser = bool(getattr(req.user, "is_superuser", False))
    grants = permission_membership_scopes(
        roles=roles,
        permission=permission,
        account_kinds={"staff", "teacher"},
    )
    return _Scope(
        is_superuser=is_superuser,
        is_unscoped=is_superuser
        or is_permission_unscoped(
            req,
            permission=permission,
            account_kinds={"staff", "teacher"},
        ),
        principal=_request_principal(request),
        grants=grants,
        branch_ids={grant.branch_id for grant in grants if grant.department_id is None},
        dept_ids={grant.department_id for grant in grants if grant.department_id is not None},
    )


# --- role grades -----------------------------------------------------------
@openapi_contract(
    path="/api/v1/tasks/grades/",
    operations=ROLE_GRADES_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def role_grades_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:read")
        _validate_query(request, allowed={"ordering", "page", "page_size"})
        _validate_ordering(request, allowed={"level", "role"})
        # No default_ordering: keep the model's compound Meta.ordering ("-level", "role")
        # when no ?ordering is given, so equal-level grades keep their deterministic
        # role tiebreak (a single-key default_ordering would drop it).
        qs = apply_filters(request, _grade_service().list(), ordering_fields=("level", "role"))
        items, total, page, size = paginate(request, qs)
        return paginated([role_grade_to_dict(g) for g in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:assign_any")
        _validate_query(request, allowed=set())
        _assert_global_grade_write(request)
        return created(
            role_grade_to_dict(
                _grade_service().create(
                    _grade_dto(
                        _body(
                            read_json(request),
                            allowed=frozenset({"role", "level", "label"}),
                        )
                    )
                )
            )
        )
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/tasks/grades/{pk}/",
    operations=ROLE_GRADE_DETAIL_CONTRACTS,
)
@csrf_exempt
@require_auth
def role_grade_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD", "PUT", "PATCH", "DELETE"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:assign_any")
    _validate_query(request, allowed=set())
    if not read:
        _assert_global_grade_write(request)
    grade = _grade_service().get(pk)
    if grade is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(role_grade_to_dict(grade))
    if request.method in ("PUT", "PATCH"):
        body = _body(
            read_json(request),
            allowed=frozenset({"role", "level", "label"}),
        )
        if request.method == "PATCH" and not body:
            raise ValidationException(
                "Provide at least one field to update.",
                code="validation_error",
                fields={"body": ["Provide at least one field to update."]},
            )
        if request.method == "PUT":
            missing = [field for field in ("role", "level") if field not in body]
            if missing:
                raise ValidationException(
                    "role and level are required for a full update.",
                    code="validation_error",
                    fields={field: ["Required."] for field in missing},
                )
        return success(role_grade_to_dict(_grade_service().update(grade, _grade_changes(body))))
    if request.method == "DELETE":
        _grade_service().delete(grade)
        return no_content()
    raise AssertionError("unreachable")


def _assert_global_grade_write(request: HttpRequest) -> None:
    if not _scope(request, permission=f"{_RESOURCE}:assign_any").is_unscoped:
        from core.exceptions import PermissionException

        raise PermissionException(
            "Role grades are organization-wide settings.",
            code="out_of_scope",
        )


def _grade_dto(body: dict[str, Any]) -> RoleGradeDTO:
    role = str_field(body, "role", max_length=32).strip()
    if not role:
        raise ValidationException(
            "Role is required.", code="validation_error", fields={"role": ["This field is required."]}
        )
    return RoleGradeDTO(role=role, level=_level(body), label=str_field(body, "label", max_length=64))


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
        changes["level"] = _level(body)
    if "label" in body:
        changes["label"] = str_field(body, "label", max_length=64)
    return changes


def _level(body: dict[str, Any]) -> int:
    value = cast(int, int_field(body, "level", required=True))
    if value < 0 or value > 1_000_000:
        raise ValidationException(
            "Level is outside the supported range.",
            code="validation_error",
            fields={"level": ["Must be between 0 and 1000000."]},
        )
    return value


# --- tasks -----------------------------------------------------------------
@openapi_contract(
    path="/api/v1/tasks/",
    operations=TASKS_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def tasks_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:read")
        s = _scope(request)
        _validate_task_filters(request)
        qs = _task_service().scoped_list(
            is_unscoped=s.is_unscoped,
            include_assignee=True,
            principal_kind=s.principal.kind,
            principal_id=s.principal.principal_id,
            branch_ids=s.branch_ids,
            dept_ids=s.dept_ids,
        )
        if request.GET.get("assignee_kind"):
            qs = qs.filter(
                assignee_principal_kind=request.GET["assignee_kind"],
                assignee_principal_id=int(request.GET["assignee_principal_id"]),
                assignee_attribution_status="captured",
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
        _validate_query(request, allowed=set())
        return _create_task(request, body=read_json(request))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/tasks/mine/",
    operations=TASKS_MINE_CONTRACTS,
)
@csrf_exempt
@require_auth
def tasks_mine_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    _validate_query(request, allowed={"page", "page_size"})
    principal = _request_principal(request)
    qs = _task_service().mine(
        principal_kind=principal.kind,
        principal_id=principal.principal_id,
    )
    items, total, page, size = paginate(request, qs)
    return paginated([task_to_dict(t) for t in items], total=total, page=page, page_size=size)


@openapi_contract(
    path="/api/v1/tasks/{pk}/",
    operations=TASK_DETAIL_CONTRACTS,
)
@csrf_exempt
@require_auth
def task_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    _validate_query(request, allowed=set())
    return success(task_to_dict(_get_visible(request, pk)))


@openapi_contract(
    path="/api/v1/tasks/{pk}/assign/",
    operations=(TASK_ASSIGN_CONTRACT,),
)
@csrf_exempt
@require_auth
def task_assign_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _validate_query(request, allowed=set())
    s = _scope(request, permission=f"{_RESOURCE}:write")
    task = _get_visible(request, pk, permission=f"{_RESOURCE}:write")
    body = _body(
        read_json(request),
        allowed=frozenset({"assignee", "assignee_principal", "department"}),
    )
    _assert_one_assignee_selector(body)
    principal_kind, principal_id = _assignee_principal(body)
    dto = AssignTaskDTO(
        assignee_provided="assignee" in body or "assignee_principal" in body,
        assignee_id=int_field(body, "assignee"),
        assignee_principal_kind=principal_kind,
        assignee_principal_id=principal_id,
        department_provided="department" in body,
        department_id=int_field(body, "department"),
    )
    result = _task_service().assign(
        task,
        dto,
        actor=request.user,
        is_unscoped=s.is_unscoped,
        write_grants=s.grants,
        assign_any_grants=_scope(request, permission=f"{_RESOURCE}:assign_any").grants,
    )
    return success(task_to_dict(result))


@openapi_contract(
    path="/api/v1/tasks/{pk}/transition/",
    operations=(TASK_TRANSITION_CONTRACT,),
)
@csrf_exempt
@require_auth
def task_transition_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    _validate_query(request, allowed=set())
    task = _get_visible(request, pk)
    to_status = str_field(
        _body(read_json(request), allowed=frozenset({"status"})),
        "status",
    )
    s = _scope(request)
    return success(
        task_to_dict(
            _task_service().transition(
                task,
                to_status=to_status,
                actor=request.user,
                actor_principal_kind=s.principal.kind,
                actor_principal_id=s.principal.principal_id,
                is_superuser=s.is_superuser,
                transition_grants=_scope(request, permission=f"{_RESOURCE}:transition_any").grants,
                assign_any_grants=_scope(request, permission=f"{_RESOURCE}:assign_any").grants,
            )
        )
    )


@openapi_contract(
    path="/api/v1/tasks/auto-assign/",
    operations=(TASK_AUTO_ASSIGN_CONTRACT,),
)
@csrf_exempt
@require_auth
def task_auto_assign_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _validate_query(request, allowed=set())
    s = _scope(request, permission=f"{_RESOURCE}:write")
    body = _body(
        read_json(request),
        allowed=frozenset({"task_ids", "department", "mode"}),
    )
    mode = str_field(body, "mode", default="fair")
    if mode not in ("fair", "free"):
        raise ValidationException(
            "Invalid mode.", code="validation_error", fields={"mode": ['Must be "fair" or "free".']}
        )
    result = _task_service().auto_assign(
        task_ids=_task_ids(body),
        department_id=int_field(body, "department", required=True),  # type: ignore[arg-type]
        actor=request.user,
        mode=mode,
        is_unscoped=s.is_unscoped,
        write_grants=s.grants,
        assign_any_grants=_scope(request, permission=f"{_RESOURCE}:assign_any").grants,
    )
    return success(result)


# --- helpers ---------------------------------------------------------------
def _get_visible(request: HttpRequest, pk: int, *, permission: str = f"{_RESOURCE}:read") -> Task:
    s = _scope(request, permission=permission)
    task = _task_service().get_visible(
        is_unscoped=s.is_unscoped,
        include_assignee=permission == f"{_RESOURCE}:read",
        principal_kind=s.principal.kind,
        principal_id=s.principal.principal_id,
        branch_ids=s.branch_ids,
        dept_ids=s.dept_ids,
        pk=pk,
    )
    if task is None:
        raise NotFoundException(code="not_found")  # not in the caller's scope -> 404, no leak
    return task


def _create_task(request: HttpRequest, *, body: dict[str, Any]) -> HttpResponse:
    s = _scope(request, permission=f"{_RESOURCE}:write")
    body = _body(body, allowed=_CREATE_FIELDS)
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
    _assert_one_assignee_selector(body)
    principal_kind, principal_id = _assignee_principal(body)
    dto = CreateTaskDTO(
        title=title,
        description=str_field(body, "description", max_length=MAX_TASK_DESCRIPTION_CHARS),
        priority=priority,
        assignee_id=int_field(body, "assignee"),
        assignee_principal_kind=principal_kind,
        assignee_principal_id=principal_id,
        department_id=int_field(body, "department"),
        branch_id=int_field(body, "branch"),
        due_at=_optional_datetime(body, "due_at"),
    )
    task = _task_service().create(
        dto,
        creator=request.user,
        creator_principal=s.principal,
        is_superuser=s.is_superuser,
        is_unscoped=s.is_unscoped,
        write_grants=s.grants,
        assign_any_grants=_scope(request, permission=f"{_RESOURCE}:assign_any").grants,
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


def _assert_one_assignee_selector(body: dict[str, Any]) -> None:
    if "assignee" in body and "assignee_principal" in body:
        raise ValidationException(
            "Choose one assignee selector.",
            code="validation_error",
            fields={
                "assignee": ["Do not combine person and role-account selectors."],
                "assignee_principal": ["Do not combine person and role-account selectors."],
            },
        )


def _assignee_principal(body: dict[str, Any]) -> tuple[str | None, int | None]:
    if "assignee_principal" not in body or body["assignee_principal"] is None:
        return None, None
    raw = body["assignee_principal"]
    if not isinstance(raw, dict) or set(raw) != {"kind", "id"}:
        raise ValidationException(
            "Invalid assignee role account.",
            code="validation_error",
            fields={"assignee_principal": ["Provide only kind and id."]},
        )
    kind = raw.get("kind")
    principal_id = raw.get("id")
    if (
        kind not in STAFF_PRINCIPAL_KINDS
        or isinstance(principal_id, bool)
        or not isinstance(principal_id, int)
        or principal_id <= 0
    ):
        raise ValidationException(
            "Invalid assignee role account.",
            code="validation_error",
            fields={"assignee_principal": ["Choose an active staff or teacher role account."]},
        )
    return str(kind), principal_id


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
    if len(out) != len(set(out)):
        raise ValidationException(
            "Duplicate task id.",
            code="validation_error",
            fields={"task_ids": ["Each task id may appear only once."]},
        )
    return out


def _body(body: dict[str, Any], *, allowed: frozenset[str]) -> dict[str, Any]:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationException(
            "Request contains unknown fields.",
            code="validation_error",
            fields={field: ["Unknown field."] for field in unknown},
        )
    return body


def _validate_query(request: HttpRequest, *, allowed: set[str]) -> None:
    unknown = sorted(set(request.GET) - allowed)
    if unknown:
        raise ValidationException(
            "Unknown query parameter.",
            code="validation_error",
            fields={field: ["Unknown query parameter."] for field in unknown},
        )
    duplicates = sorted(name for name in request.GET if len(request.GET.getlist(name)) != 1)
    if duplicates:
        raise ValidationException(
            "Query parameter may be supplied only once.",
            code="validation_error",
            fields={field: ["Supply this parameter once."] for field in duplicates},
        )
    validate_pagination_filters(request)


def _validate_ordering(request: HttpRequest, *, allowed: set[str]) -> None:
    if "ordering" not in request.GET:
        return
    ordering = request.GET.get("ordering", "")
    field = ordering[1:] if ordering.startswith("-") else ordering
    if field not in allowed:
        raise ValidationException(
            "Invalid ordering.",
            code="validation_error",
            fields={"ordering": ["Choose a declared ordering field."]},
        )


def _validate_task_filters(request: HttpRequest) -> None:
    _validate_query(
        request,
        allowed={
            "status",
            "priority",
            "assignee",
            "assignee_kind",
            "assignee_principal_id",
            "department",
            "branch",
            "search",
            "ordering",
            "page",
            "page_size",
        },
    )
    status = request.GET.get("status")
    if "status" in request.GET and status not in Task.Status.values:
        raise ValidationException(
            "Invalid status filter.",
            code="validation_error",
            fields={"status": [f"Must be one of: {', '.join(Task.Status.values)}."]},
        )
    priority = request.GET.get("priority")
    if "priority" in request.GET and priority not in Task.Priority.values:
        raise ValidationException(
            "Invalid priority filter.",
            code="validation_error",
            fields={"priority": [f"Must be one of: {', '.join(Task.Priority.values)}."]},
        )
    for name in ("assignee", "department", "branch"):
        if name in request.GET and positive_int_filter(request, name) is None:
            raise ValidationException(
                f"Invalid {name} filter.",
                code="validation_error",
                fields={name: ["Must be a positive integer."]},
            )
    _validate_ordering(request, allowed={"created_at", "due_at", "priority"})
    search = request.GET.get("search")
    if search is not None and len(search) > 200:
        raise ValidationException(
            "Search term is too long.",
            code="validation_error",
            fields={"search": ["Must be at most 200 characters."]},
        )
    assignee_kind = request.GET.get("assignee_kind", "").strip()
    raw_principal_id = request.GET.get("assignee_principal_id", "").strip()
    exact_filter_supplied = "assignee_kind" in request.GET or "assignee_principal_id" in request.GET
    if exact_filter_supplied and (not assignee_kind or not raw_principal_id):
        raise ValidationException(
            "Assignee role filters must be provided together.",
            code="validation_error",
            fields={
                "assignee_kind": ["Provide both role filters."],
                "assignee_principal_id": ["Provide both role filters."],
            },
        )
    if assignee_kind:
        if assignee_kind not in STAFF_PRINCIPAL_KINDS:
            raise ValidationException(
                "Invalid assignee kind.",
                code="validation_error",
                fields={"assignee_kind": ["Must be staff or teacher."]},
            )
        positive_int_filter(request, "assignee_principal_id")
