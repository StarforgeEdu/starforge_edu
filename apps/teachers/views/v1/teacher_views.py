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
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.teachers.dto.teacher_dto import TeacherCreateDTO
from apps.teachers.filters import apply_teacher_directory_filters
from apps.teachers.interfaces.teacher_service import ITeacherService
from apps.teachers.models import TeacherProfile
from apps.teachers.openapi_contracts import PAYOUT_POLICY_CONTRACTS, PREPARE_SALARY_CONTRACT
from apps.teachers.presenters import teacher_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, decimal_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.openapi_contracts import openapi_contract
from core.permissions import _request_overrides, get_user_roles, has_permission_code
from core.responses import created, error, no_content, paginated, success, validation_error
from core.scoping import (
    assert_permission_membership_scope,
    is_permission_unscoped,
    permission_membership_scope_q,
    request_permission_membership_allows,
    scope_to_permission_memberships,
)

_RESOURCE = "teachers"
_FILTERS = ("branch", "department", "is_substitute")
_SEARCH = ("first_name", "last_name", "phone")
_ORDERING = ("created_at", "hire_date")
_SCALARS = ("hire_date", "subjects", "qualifications", "salary_type", "rate", "is_substitute")
_COMPENSATION_FIELDS = frozenset({"salary_type", "rate"})
_CREATE_FIELDS = frozenset(
    {
        "account_type",
        "birthdate",
        "branch",
        "department",
        "email",
        "first_name",
        "gender",
        "hire_date",
        "is_substitute",
        "last_name",
        "middle_name",
        "phone",
        "qualifications",
        "rate",
        "salary_type",
        "subjects",
        "username",
    }
)
_UPDATE_FIELDS = frozenset(
    {
        "birthdate",
        "branch",
        "department",
        "email",
        "first_name",
        "gender",
        "hire_date",
        "is_active",
        "is_substitute",
        "last_name",
        "middle_name",
        "phone",
        "qualifications",
        "rate",
        "salary_type",
        "subjects",
    }
)


def _service() -> ITeacherService:
    return container.resolve(ITeacherService)  # type: ignore[type-abstract]


def _has_compensation_permission(
    request: HttpRequest,
    permission: str = "compensation:read",
) -> bool:
    if getattr(request.user, "is_superuser", False):
        return True
    roles = get_user_roles(request)
    overrides = _request_overrides(request) if roles.fallback_roles else {}
    return has_permission_code(roles, permission, overrides)


def _can_view_compensation(request: HttpRequest, teacher: TeacherProfile) -> bool:
    """Require compensation authority over this teacher's exact scope.

    A compensation grant in one branch must not reveal pay for teachers made visible by
    a separate faculty grant in another branch.
    """
    return request_permission_membership_allows(
        request,
        permission="compensation:read",
        branch_id=teacher.branch_id,
        department_id=teacher.department_id,
        account_kinds={"staff"},
    )


def _require_compensation_write(
    request: HttpRequest,
    *,
    branch_id: int,
    department_id: int | None,
) -> None:
    """Protect hidden compensation values from a blind directory write."""
    check_perm(request, "compensation:write")
    assert_permission_membership_scope(
        request,
        permission="compensation:write",
        branch_id=branch_id,
        department_id=department_id,
        account_kinds={"staff"},
    )


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
    permission = f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write"
    check_perm(request, permission)
    teacher = _service().get(pk)
    if teacher is None:
        raise NotFoundException(code="not_found")
    assert_permission_membership_scope(
        request,
        permission=permission,
        branch_id=teacher.branch_id,
        department_id=teacher.department_id,
        account_kinds={"staff"},
    )

    if request.method in ("GET", "HEAD"):
        return success(
            teacher_to_dict(
                teacher,
                include_compensation=_can_view_compensation(request, teacher),
            )
        )
    if request.method in ("PUT", "PATCH"):
        body = read_json(request)
        _reject_unknown_fields(body, allowed=_UPDATE_FIELDS, operation="teacher update")
        changes = _changes(body)
        target_branch_id = changes.get("branch", teacher.branch_id)
        target_department_id = changes.get("department", teacher.department_id)
        if "branch" in changes or "department" in changes:
            assert_permission_membership_scope(
                request,
                permission=f"{_RESOURCE}:write",
                branch_id=target_branch_id,
                department_id=target_department_id,
                account_kinds={"staff"},
            )
            # Compensation history and the active payout policy follow the
            # teacher.  Moving the profile without pay authority would let a
            # faculty-only writer move hidden salary data into a branch where
            # another membership can read it.  Require pay-write authority at
            # both the old and new boundaries.
            _require_compensation_write(
                request,
                branch_id=teacher.branch_id,
                department_id=teacher.department_id,
            )
            _require_compensation_write(
                request,
                branch_id=target_branch_id,
                department_id=target_department_id,
            )
        if _COMPENSATION_FIELDS.intersection(changes):
            _require_compensation_write(
                request,
                branch_id=target_branch_id,
                department_id=target_department_id,
            )
        updated = _service().update(teacher, changes)
        return success(
            teacher_to_dict(
                updated,
                include_compensation=_can_view_compensation(request, updated),
            )
        )
    if request.method == "DELETE":
        # Hard deletion also removes the payout policy and historical profile
        # rate fields.  Treat it as a compensation mutation as well as a
        # faculty mutation; a directory editor alone must not erase payroll
        # evidence.
        _require_compensation_write(
            request,
            branch_id=teacher.branch_id,
            department_id=teacher.department_id,
        )
        _service().delete(teacher)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def teacher_dashboard_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return success(_service().dashboard(request.user, get_user_roles(request)))


def _teacher_in_scope(
    request: HttpRequest,
    pk: int,
    *,
    permission: str,
) -> TeacherProfile:
    teacher = _service().get(pk)
    if teacher is None:
        raise NotFoundException(code="not_found")
    assert_permission_membership_scope(
        request,
        permission=permission,
        branch_id=teacher.branch_id,
        department_id=teacher.department_id,
        account_kinds={"staff"},
    )
    return teacher


@csrf_exempt
@require_auth
def teacher_credentials_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Issue a one-time teacher password; the raw value is never stored or repeated."""
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    teacher = _teacher_in_scope(request, pk, permission=f"{_RESOURCE}:write")
    from apps.users.services import issue_role_credentials

    return success(
        issue_role_credentials(
            teacher,
            actor=request.user,
            resource_type="teachers.TeacherProfile",
        )
    )


@openapi_contract(
    path="/api/v1/teachers/{pk}/payout-policy/",
    operations=PAYOUT_POLICY_CONTRACTS,
)
@csrf_exempt
@require_auth
def teacher_payout_policy_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Read or configure one teacher's dynamic pay rule.

    Compensation is intentionally independent of the faculty-directory grant:
    payroll operators need not receive the broader ``teachers:*`` capability,
    and faculty editors cannot infer or overwrite pay through this route.
    """
    from apps.teachers.models import PayoutPolicy
    from apps.teachers.presenters import payout_policy_to_dict

    read = request.method in ("GET", "HEAD")
    compensation_permission = "compensation:read" if read else "compensation:write"
    check_perm(request, compensation_permission)
    teacher = _teacher_in_scope(
        request,
        pk,
        permission=compensation_permission,
    )
    if read:
        policy = PayoutPolicy.objects.filter(teacher=teacher).first()
        if policy is None:
            raise NotFoundException(_("This teacher has no payout policy yet."), code="no_payout_policy")
        return success(payout_policy_to_dict(policy))
    if request.method in ("PUT", "POST"):
        from apps.teachers.services import set_payout_policy

        body = read_json(request)
        allowed_fields = {
            "method",
            "hourly_rate_uzs",
            "flat_amount_uzs",
            "tuition_percent",
            "is_active",
        }
        unknown_fields = sorted(set(body) - allowed_fields)
        if unknown_fields:
            raise ValidationException(
                _("Unsupported payout-policy field."),
                code="validation_error",
                fields={field: [_("This field is not supported.")] for field in unknown_fields},
            )
        for decimal_name in ("hourly_rate_uzs", "flat_amount_uzs", "tuition_percent"):
            raw_decimal = body.get(decimal_name)
            if raw_decimal is not None and not isinstance(raw_decimal, str):
                raise ValidationException(
                    _("Decimal values must be strings."),
                    code="validation_error",
                    fields={decimal_name: [_("Use a decimal string, never a JSON number.")]},
                )
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


@openapi_contract(
    path="/api/v1/teachers/{pk}/prepare-salary/",
    operations=(PREPARE_SALARY_CONTRACT,),
)
@csrf_exempt
@require_auth
def teacher_prepare_salary_view(request: HttpRequest, pk: int) -> HttpResponse:
    """F13-1: compute the teacher's payout for a period from their policy and raise it as an
    A-1 salary-prep request. Preparing pay is a separate capability from
    editing either the teacher profile or ordinary finance records."""
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "compensation:run")
    teacher = _teacher_in_scope(request, pk, permission="compensation:run")
    body = read_json(request)
    unknown_fields = sorted(set(body) - {"period_start", "period_end"})
    if unknown_fields:
        raise ValidationException(
            _("Unsupported salary preparation field."),
            code="validation_error",
            fields={field: [_("This field is not supported.")] for field in unknown_fields},
        )
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is None:
        raise ValidationException(
            _("Idempotency-Key is required."),
            code="validation_error",
            fields={"Idempotency-Key": [_("This header is required.")]},
        )
    start = _date(body, "period_start")
    end = _date(body, "period_end")
    if start is None or end is None:
        return validation_error({"period_start": ["period_start and period_end are required."]})
    from apps.teachers.services import prepare_salary

    req = prepare_salary(
        teacher=teacher,
        period_start=start,
        period_end=end,
        requested_by=request.user,
        idempotency_key=idempotency_key,
    )
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
    qs = _service().list()
    if not is_permission_unscoped(
        request,
        permission=f"{_RESOURCE}:read",
        account_kinds={"staff"},
    ):
        qs = qs.filter(
            permission_membership_scope_q(
                roles=get_user_roles(request),
                permission=f"{_RESOURCE}:read",
                branch_field="branch_id",
                department_field="department_id",
                account_kinds={"staff"},
            )
        )
    qs = apply_filters(
        request,
        qs,
        filter_fields=_FILTERS,
        search_fields=_SEARCH,
        ordering_fields=_ORDERING,
        default_ordering="-created_at",
    )
    has_compensation_permission = _has_compensation_permission(request)
    if request.GET.get("salary_type") not in (None, "") and has_compensation_permission:
        qs = scope_to_permission_memberships(
            request,
            qs,
            permission="compensation:read",
            branch_field="branch_id",
            department_field="department_id",
            account_kinds={"staff"},
        )
    qs = apply_teacher_directory_filters(
        request,
        qs,
        can_view_compensation=has_compensation_permission,
    )
    items, total, page, size = paginate(request, qs)
    return paginated(
        [
            teacher_to_dict(
                teacher,
                include_compensation=_can_view_compensation(request, teacher),
            )
            for teacher in items
        ],
        total=total,
        page=page,
        page_size=size,
    )


def _create(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    _reject_unknown_fields(body, allowed=_CREATE_FIELDS, operation="teacher creation")
    phone = str_field(body, "phone")
    email = str_field(body, "email")
    if not phone and not email:
        return validation_error({"phone": ["Provide a phone or an email."]})
    branch_id = int_field(body, "branch", required=True)
    department_id = int_field(body, "department")
    assert_permission_membership_scope(
        request,
        permission=f"{_RESOURCE}:write",
        branch_id=branch_id,
        department_id=department_id,
        account_kinds={"staff"},
    )
    if _COMPENSATION_FIELDS.intersection(body):
        _require_compensation_write(
            request,
            branch_id=branch_id,  # type: ignore[arg-type]
            department_id=department_id,
        )
    dto = TeacherCreateDTO(
        branch_id=branch_id,  # type: ignore[arg-type]
        account_type_id=int_field(body, "account_type"),
        department_id=department_id,
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
    teacher = _service().create(dto)
    return created(
        teacher_to_dict(
            teacher,
            include_compensation=_can_view_compensation(request, teacher),
        )
    )


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


def _reject_unknown_fields(
    body: dict[str, Any],
    *,
    allowed: frozenset[str],
    operation: str,
) -> None:
    unknown_fields = sorted(set(body) - allowed)
    if not unknown_fields:
        return
    raise ValidationException(
        _("Unsupported field in %(operation)s.") % {"operation": operation},
        code="validation_error",
        fields={field: [_("This field is not supported.")] for field in unknown_fields},
    )


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
