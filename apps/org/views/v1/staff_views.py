"""Role-native staff-account administration API."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt

from apps.org.models import Branch, Department, StaffProfile
from apps.org.presenters import staff_to_dict
from apps.org.services import STAFF_ROLES, create_staff_account, deactivate_staff_account
from core.api_auth import check_perm, require_auth
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.responses import created, error, no_content, paginated, success
from core.scoping import assert_branch_id_in_scope, branch_ids, is_unscoped

_SEARCH = ("username", "first_name", "last_name", "phone", "email")
_ORDERING = ("created_at", "last_name", "first_name", "username")


def _query(request: HttpRequest):
    qs = StaffProfile.objects.select_related("user").prefetch_related("user__role_memberships")
    if not is_unscoped(request):
        qs = qs.filter(
            user__role_memberships__branch_id__in=branch_ids(request),
            user__role_memberships__revoked_at__isnull=True,
        )
    return qs.distinct()


def _get(request: HttpRequest, pk: int) -> StaffProfile:
    staff = _query(request).filter(pk=pk).first()
    if staff is None:
        raise NotFoundException(code="not_found")
    return staff


def _date(body: dict[str, Any], name: str):
    raw = body.get(name)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        parsed = None
    else:
        try:
            parsed = parse_date(raw)
        except ValueError:
            parsed = None
    if parsed is None:
        raise ValidationException(
            "Invalid date.",
            code="validation_error",
            fields={name: ["Enter a valid date (YYYY-MM-DD)."]},
        )
    return parsed


def _gender(body: dict[str, Any]) -> str:
    value = str_field(body, "gender")
    if value and value not in StaffProfile.Gender.values:
        raise ValidationException(
            "Invalid gender.",
            code="validation_error",
            fields={"gender": ["Not a valid choice."]},
        )
    return value


def _branch(branch_id: int | None) -> Branch:
    if branch_id is None:
        raise ValidationException("Invalid branch.", code="invalid_branch", fields={"branch": ["Not found."]})
    branch = Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
    if branch is None:
        raise ValidationException("Invalid branch.", code="invalid_branch", fields={"branch": ["Not found."]})
    return branch


def _department(department_id: int | None) -> Department | None:
    if department_id is None:
        return None
    department = Department.objects.filter(pk=department_id).first()
    if department is None:
        raise ValidationException(
            "Invalid department.",
            code="invalid_department",
            fields={"department": ["Not found."]},
        )
    return department


@csrf_exempt
@require_auth
def staff_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "users:read")
        qs = apply_filters(
            request,
            _query(request),
            filter_fields=("is_active",),
            search_fields=_SEARCH,
            ordering_fields=_ORDERING,
            default_ordering="last_name",
        )
        role = request.GET.get("role", "").strip()
        if role:
            if role not in STAFF_ROLES:
                raise ValidationException(
                    "Invalid role.", code="validation_error", fields={"role": ["Not a staff role."]}
                )
            qs = qs.filter(
                user__role_memberships__role=role,
                user__role_memberships__revoked_at__isnull=True,
            ).distinct()
        items, total, page, size = paginate(request, qs)
        return paginated(
            [staff_to_dict(staff) for staff in items],
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        check_perm(request, "users:write")
        body = read_json(request)
        phone, email = str_field(body, "phone"), str_field(body, "email")
        if not phone and not email:
            raise ValidationException(
                "Provide a phone or an email.",
                code="validation_error",
                fields={"phone": ["Provide a phone or an email."]},
            )
        branch_id = int_field(body, "branch", required=True)
        assert_branch_id_in_scope(request, branch_id)
        staff = create_staff_account(
            branch=_branch(branch_id),
            department=_department(int_field(body, "department")),
            role=str_field(body, "role"),
            username=str_field(body, "username"),
            phone=phone,
            email=email,
            first_name=str_field(body, "first_name"),
            last_name=str_field(body, "last_name"),
            middle_name=str_field(body, "middle_name"),
            birthdate=_date(body, "birthdate"),
            gender=_gender(body),
        )
        staff = _query(request).get(pk=staff.pk)
        return created(staff_to_dict(staff))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def staff_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "users:read" if read else "users:write")
    staff = _get(request, pk)
    if read:
        return success(staff_to_dict(staff))
    if request.method in ("PUT", "PATCH"):
        body = read_json(request)
        changes: dict[str, Any] = {
            field: str_field(body, field)
            for field in ("first_name", "last_name", "middle_name", "phone", "email")
            if field in body
        }
        if "birthdate" in body:
            changes["birthdate"] = _date(body, "birthdate")
        if "gender" in body:
            changes["gender"] = _gender(body)
        if "is_active" in body:
            changes["is_active"] = bool_field(body, "is_active")
        if changes:
            from apps.users.services import update_role_identity

            update_role_identity(staff, changes)
        if any(field in body for field in ("role", "branch", "department")):
            membership = staff.user.role_memberships.filter(revoked_at__isnull=True).order_by("id").first()
            if membership is None:
                raise ValidationException("Staff account has no active role.", code="missing_role")
            role = str_field(body, "role", default=membership.role)
            if role not in STAFF_ROLES:
                raise ValidationException(
                    "Invalid role.",
                    code="validation_error",
                    fields={"role": ["Not a staff role."]},
                )
            branch_id = int_field(body, "branch", required=True) if "branch" in body else membership.branch_id
            assert_branch_id_in_scope(request, branch_id)
            branch = _branch(branch_id)
            department = (
                _department(int_field(body, "department")) if "department" in body else membership.department
            )
            if department is not None and department.branch_id != branch.pk:
                raise ValidationException(
                    "Department must belong to the selected branch.",
                    code="department_branch_mismatch",
                )
            membership.role = role
            membership.branch = branch
            membership.department = department
            membership.save(update_fields=["role", "branch", "department"])
        refreshed = _query(request).get(pk=staff.pk)
        return success(staff_to_dict(refreshed))
    if request.method == "DELETE":
        deactivate_staff_account(staff)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def staff_credentials_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "users:write")
    staff = _get(request, pk)
    from apps.users.services import issue_role_credentials

    return success(
        issue_role_credentials(
            staff,
            actor=request.user,
            resource_type="org.StaffProfile",
        )
    )
