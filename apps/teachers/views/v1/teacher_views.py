"""Teacher endpoints — plain Django views over the layered architecture.

Collection (GET list / POST create) and detail (GET / PUT / PATCH / DELETE) dispatch on
method so the required perm tracks it (teachers:read for reads, teachers:write for
writes). Branch scoping mirrors the old ObjectScopedPermission: lists are scoped, detail/
write asserts the object is in the caller's branches, create asserts the target branch is.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.teachers.dto.teacher_dto import TeacherCreateDTO
from apps.teachers.interfaces.teacher_service import ITeacherService
from apps.teachers.models import TeacherProfile
from apps.teachers.presenters import teacher_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, decimal_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, no_content, paginated, success, validation_error
from core.scoping import assert_branch_id_in_scope, assert_in_branch_scope, scope_to_branches

_RESOURCE = "teachers"
_FILTERS = ("branch", "department", "is_substitute")
_SEARCH = ("first_name", "last_name", "phone")
_ORDERING = ("created_at", "hire_date")
_SCALARS = ("hire_date", "subjects", "qualifications", "salary_type", "rate", "is_substitute")


def _service() -> ITeacherService:
    return container.resolve(ITeacherService)  # type: ignore[type-abstract]


@csrf_exempt
@require_auth
def teachers_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        return _list(request)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def teacher_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    teacher = _service().get(pk)
    if teacher is None:
        raise NotFoundException(code="not_found")
    assert_in_branch_scope(request, teacher)  # branch-scoped role can't touch another branch

    if request.method in ("GET", "HEAD"):
        return success(teacher_to_dict(teacher))
    if request.method in ("PUT", "PATCH"):
        changes = _changes(read_json(request))
        if "branch" in changes:  # reassignment must land in a branch the caller can reach
            assert_branch_id_in_scope(request, changes["branch"])
        updated = _service().update(teacher, changes)
        return success(teacher_to_dict(updated))
    if request.method == "DELETE":
        _service().delete(teacher)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def teacher_dashboard_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return success(_service().dashboard(request.user, get_user_roles(request)))


def _teacher_in_scope(request: HttpRequest, pk: int) -> TeacherProfile:
    teacher = _service().get(pk)
    if teacher is None:
        raise NotFoundException(code="not_found")
    assert_in_branch_scope(request, teacher)
    return teacher


@csrf_exempt
@require_auth
def teacher_credentials_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Issue a one-time teacher password; the raw value is never stored or repeated."""
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    teacher = _teacher_in_scope(request, pk)
    from apps.users.services import issue_role_credentials

    return success(
        issue_role_credentials(
            teacher,
            actor=request.user,
            resource_type="teachers.TeacherProfile",
        )
    )


@csrf_exempt
@require_auth
def teacher_payout_policy_view(request: HttpRequest, pk: int) -> HttpResponse:
    """F13-1: GET / set a teacher's dynamic pay rule (method + params). teachers:read to
    view, teachers:write to configure — the manager sets HOW the teacher is paid."""
    from apps.teachers.models import PayoutPolicy
    from apps.teachers.presenters import payout_policy_to_dict

    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    teacher = _teacher_in_scope(request, pk)
    if read:
        policy = PayoutPolicy.objects.filter(teacher=teacher).first()
        if policy is None:
            raise NotFoundException("This teacher has no payout policy yet.", code="no_payout_policy")
        return success(payout_policy_to_dict(policy))
    if request.method in ("PUT", "POST"):
        from apps.teachers.services import set_payout_policy

        body = read_json(request)
        policy = set_payout_policy(
            teacher=teacher,
            method=str_field(body, "method"),
            hourly_rate_uzs=body.get("hourly_rate_uzs"),
            flat_amount_uzs=body.get("flat_amount_uzs"),
            tuition_percent=body.get("tuition_percent"),
            is_active=bool_field(body, "is_active", default=True),
        )
        return success(payout_policy_to_dict(policy))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def teacher_prepare_salary_view(request: HttpRequest, pk: int) -> HttpResponse:
    """F13-1: compute the teacher's payout for a period from their policy and raise it as an
    A-1 salary-prep request (a manager then approves + a cashier disburses). teachers:write."""
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    teacher = _teacher_in_scope(request, pk)
    body = read_json(request)
    start = _date(body, "period_start")
    end = _date(body, "period_end")
    if start is None or end is None:
        return validation_error({"period_start": ["period_start and period_end are required."]})
    from apps.teachers.services import prepare_salary

    req = prepare_salary(teacher=teacher, period_start=start, period_end=end, requested_by=request.user)
    return created(
        {
            "request_id": req.pk,
            "kind": req.kind,
            "amount_uzs": str(req.amount_uzs),
            "status": req.status,
            "breakdown": req.payload.get("breakdown"),
        }
    )


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
    return paginated([teacher_to_dict(t) for t in items], total=total, page=page, page_size=size)


def _create(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    phone = str_field(body, "phone")
    email = str_field(body, "email")
    if not phone and not email:
        return validation_error({"phone": ["Provide a phone or an email."]})
    branch_id = int_field(body, "branch", required=True)
    assert_branch_id_in_scope(request, branch_id)  # create-scope (no object yet)
    dto = TeacherCreateDTO(
        branch_id=branch_id,  # type: ignore[arg-type]
        department_id=int_field(body, "department"),
        username=str_field(body, "username"),
        phone=phone,
        email=email,
        first_name=str_field(body, "first_name"),
        last_name=str_field(body, "last_name"),
        middle_name=str_field(body, "middle_name"),
        birthdate=_date(body, "birthdate"),
        gender=_gender(body),
        hire_date=_date(body, "hire_date"),
        subjects=_list_field(body, "subjects"),
        qualifications=str_field(body, "qualifications"),
        salary_type=_salary_type(body),
        rate=decimal_field(body, "rate", max_digits=12),
        is_substitute=bool_field(body, "is_substitute"),
    )
    return created(teacher_to_dict(_service().create(dto)))


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    """The provided updatable fields only (PATCH-correct: absent vs null differ)."""
    changes: dict[str, Any] = {}
    for field in ("first_name", "last_name", "middle_name", "phone", "email"):
        if field in body:
            changes[field] = str_field(body, field)
    if "birthdate" in body:
        changes["birthdate"] = _date(body, "birthdate")
    if "gender" in body:
        changes["gender"] = _gender(body)
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active")
    if "branch" in body:
        changes["branch"] = int_field(body, "branch", required=True)
    if "department" in body:
        changes["department"] = int_field(body, "department")
    if "hire_date" in body:
        changes["hire_date"] = _date(body, "hire_date")
    if "subjects" in body:
        changes["subjects"] = _list_field(body, "subjects")
    if "qualifications" in body:
        changes["qualifications"] = str_field(body, "qualifications")
    if "salary_type" in body:
        changes["salary_type"] = _salary_type(body)
    if "rate" in body:
        changes["rate"] = decimal_field(body, "rate", max_digits=12)
    if "is_substitute" in body:
        changes["is_substitute"] = bool_field(body, "is_substitute")
    return changes


def _date(body: dict[str, Any], name: str) -> date | None:
    raw = body.get(name)
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise ValidationException(
            "Invalid date.", code="validation_error", fields={name: ["Must be an ISO date."]}
        ) from None


def _gender(body: dict[str, Any]) -> str:
    """Optional gender via the profile's own choice set; blank allowed, 400 on a bad value."""
    value = str_field(body, "gender")
    if value == "":
        return ""
    if value not in TeacherProfile.Gender.values:
        raise ValidationException(
            "Invalid gender.",
            code="validation_error",
            fields={"gender": ["Not a valid choice."]},
        )
    return value


def _salary_type(body: dict[str, Any]) -> str:
    value = str_field(body, "salary_type", default="monthly")
    if value not in TeacherProfile.SalaryType.values:
        raise ValidationException(
            "Invalid salary_type.",
            code="validation_error",
            fields={"salary_type": ["Not a valid choice."]},
        )
    return value


def _list_field(body: dict[str, Any], name: str) -> list:
    raw = body.get(name, [])
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValidationException(
            "Invalid list.", code="validation_error", fields={name: ["Must be a list."]}
        )
    return raw
