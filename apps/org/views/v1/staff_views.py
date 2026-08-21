"""Role-native staff-account administration API."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt

from apps.access.models import AccountType
from apps.org.models import Branch, Department, StaffProfile
from apps.org.presenters import staff_directory_row_to_dict, staff_to_dict
from apps.org.services import STAFF_ROLES, create_staff_account, deactivate_staff_account
from apps.users.models import RoleMembership
from core.api_auth import check_perm, require_auth
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, no_content, paginated, success
from core.scoping import (
    assert_permission_membership_scope,
    assert_permission_organization_scope,
    is_permission_unscoped,
    permission_membership_scope_q,
    request_permission_membership_allows,
)

_SEARCH = ("username", "first_name", "last_name", "phone", "email")
_ORDERING = ("created_at", "last_name", "first_name", "username")
_CREATE_FIELDS = frozenset(
    {
        "account_type",
        "birthdate",
        "branch",
        "department",
        "email",
        "first_name",
        "gender",
        "last_name",
        "middle_name",
        "phone",
        "role",
        "username",
    }
)
_UPDATE_FIELDS = frozenset(
    {
        "account_type",
        "assignment",
        "birthdate",
        "branch",
        "department",
        "email",
        "first_name",
        "gender",
        "is_active",
        "last_name",
        "middle_name",
        "phone",
        "role",
    }
)


def _active_staff_memberships():
    return RoleMembership.objects.filter(revoked_at__isnull=True).filter(
        Q(
            account_type__account_kind=AccountType.AccountKind.STAFF,
            account_type__is_active=True,
        )
        | Q(account_type__isnull=True, role__in=STAFF_ROLES)
    )


def _visible_staff_memberships(request: HttpRequest, permission: str):
    memberships = _active_staff_memberships()
    if not is_permission_unscoped(
        request,
        permission=permission,
        account_kinds={AccountType.AccountKind.STAFF},
    ):
        memberships = memberships.filter(
            permission_membership_scope_q(
                roles=get_user_roles(request),
                permission=permission,
                branch_field="branch_id",
                department_field="department_id",
                account_kinds={AccountType.AccountKind.STAFF},
            )
        )
    return memberships.select_related("account_type", "branch", "department").order_by("id")


def _query(request: HttpRequest, permission: str):
    visible_memberships = _visible_staff_memberships(request, permission)
    qs = StaffProfile.objects.select_related("user").prefetch_related(
        Prefetch(
            "user__role_memberships",
            queryset=visible_memberships,
            to_attr="_visible_staff_memberships",
        )
    )
    if not is_permission_unscoped(
        request,
        permission=permission,
        account_kinds={AccountType.AccountKind.STAFF},
    ):
        qs = qs.annotate(
            _has_visible_staff_membership=Exists(visible_memberships.filter(user_id=OuterRef("user_id")))
        ).filter(_has_visible_staff_membership=True)
    return qs


def _filter_by_visible_membership(
    queryset,
    request: HttpRequest,
    permission: str,
    **membership_filters: Any,
):
    matches = _visible_staff_memberships(request, permission).filter(
        user_id=OuterRef("user_id"),
        **membership_filters,
    )
    return queryset.annotate(_matches_staff_membership=Exists(matches)).filter(_matches_staff_membership=True)


def _assert_can_manage_entire_staff_account(
    request: HttpRequest,
    staff: StaffProfile,
    *,
    permission: str,
) -> None:
    """Require authority over every assignment before changing global identity."""
    if is_permission_unscoped(
        request,
        permission=permission,
        account_kinds={AccountType.AccountKind.STAFF},
    ):
        return
    for membership in _active_staff_memberships().filter(user_id=staff.user_id):
        if not request_permission_membership_allows(
            request,
            permission=permission,
            branch_id=membership.branch_id,
            department_id=membership.department_id,
            account_kinds={AccountType.AccountKind.STAFF},
        ):
            # Hide a responsibility outside the caller's scope.
            raise NotFoundException(code="not_found")


def _get(request: HttpRequest, pk: int, permission: str) -> StaffProfile:
    staff = _query(request, permission).filter(pk=pk).first()
    if staff is None:
        raise NotFoundException(code="not_found")
    return staff


def _reject_unknown_fields(body: dict[str, Any], *, allowed: frozenset[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationException(
            "Unsupported staff-account field.",
            code="validation_error",
            fields={field: ["This field is not supported."] for field in unknown},
        )


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


def _staff_account_type(data: dict[str, Any], *, required: bool) -> AccountType | None:
    account_type_id = int_field(data, "account_type")
    if account_type_id is not None:
        account_type = AccountType.objects.filter(
            pk=account_type_id,
            account_kind=AccountType.AccountKind.STAFF,
            is_active=True,
        ).first()
        if account_type is None:
            raise ValidationException(
                "Invalid account type.",
                code="invalid_account_type",
                fields={"account_type": ["Choose an active staff account type."]},
            )
        return account_type

    # Temporary request compatibility for clients that still send the old role
    # field. Responses and the admin surface are AccountType-native.
    role = str_field(data, "role")
    if role:
        account_type = AccountType.objects.filter(
            is_system=True,
            is_active=True,
            account_kind=AccountType.AccountKind.STAFF,
            slug=role,
        ).first()
        if account_type is None:
            raise ValidationException(
                "Invalid account type.",
                code="invalid_account_type",
                fields={"account_type": ["The matching system account type is unavailable."]},
            )
        return account_type
    if required:
        raise ValidationException(
            "Account type is required.",
            code="validation_error",
            fields={"account_type": ["This field is required."]},
        )
    return None


@csrf_exempt
@require_auth
def staff_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "users:read")
        qs = apply_filters(
            request,
            _query(request, "users:read"),
            filter_fields=("is_active",),
            search_fields=_SEARCH,
            ordering_fields=_ORDERING,
            default_ordering="last_name",
        )
        account_type_raw = request.GET.get("account_type", "").strip()
        if account_type_raw:
            try:
                account_type_id = int(account_type_raw)
            except ValueError:
                raise ValidationException(
                    "Invalid account type.",
                    code="invalid_query_param",
                    fields={"account_type": ["Must be an integer."]},
                ) from None
            qs = _filter_by_visible_membership(
                qs,
                request,
                "users:read",
                account_type_id=account_type_id,
            )
        else:
            role = request.GET.get("role", "").strip()
            if role:
                if role not in STAFF_ROLES:
                    raise ValidationException(
                        "Invalid role.", code="validation_error", fields={"role": ["Not a staff role."]}
                    )
                qs = _filter_by_visible_membership(
                    qs,
                    request,
                    "users:read",
                    account_type__is_system=True,
                    account_type__slug=role,
                )
        items, total, page, size = paginate(request, qs)
        return paginated(
            [staff_directory_row_to_dict(staff) for staff in items],
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        check_perm(request, "users:write")
        # Creating a staff account also creates an authorization assignment.
        # Prevent a scoped directory editor from minting an owner or other
        # privileged account type.
        check_perm(request, "access:write")
        assert_permission_organization_scope(
            request,
            permission="access:write",
            account_kinds={AccountType.AccountKind.STAFF},
        )
        body = read_json(request)
        _reject_unknown_fields(body, allowed=_CREATE_FIELDS)
        phone, email = str_field(body, "phone"), str_field(body, "email")
        if not phone and not email:
            raise ValidationException(
                "Provide a phone or an email.",
                code="validation_error",
                fields={"phone": ["Provide a phone or an email."]},
            )
        branch_id = int_field(body, "branch", required=True)
        assert_permission_membership_scope(
            request,
            permission="users:write",
            branch_id=branch_id,
            enforce_department=False,
        )
        staff = create_staff_account(
            branch=_branch(branch_id),
            department=_department(int_field(body, "department")),
            account_type=_staff_account_type(body, required=True),
            username=str_field(body, "username"),
            phone=phone,
            email=email,
            first_name=str_field(body, "first_name"),
            last_name=str_field(body, "last_name"),
            middle_name=str_field(body, "middle_name"),
            birthdate=_date(body, "birthdate"),
            gender=_gender(body),
        )
        staff = _query(request, "users:write").get(pk=staff.pk)
        return created(staff_to_dict(staff))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
@transaction.atomic
def staff_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "users:read" if read else "users:write")
    permission = "users:read" if read else "users:write"
    staff = _get(request, pk, permission)
    if read:
        return success(staff_to_dict(staff))
    if request.method in ("PUT", "PATCH"):
        body = read_json(request)
        _reject_unknown_fields(body, allowed=_UPDATE_FIELDS)
        responsibility_fields = {
            "account_type",
            "assignment",
            "branch",
            "department",
            "role",
        }.intersection(body)
        if responsibility_fields:
            # Responsibilities have their own audited create/revoke API. This
            # profile endpoint cannot guess which assignment a multi-scope
            # staff account intended to replace.
            raise ValidationException(
                "Manage responsibilities through account-type assignments.",
                code="use_account_type_assignments",
                fields={
                    field: ["This field is managed by the responsibilities workflow."]
                    for field in sorted(responsibility_fields)
                },
            )
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
            _assert_can_manage_entire_staff_account(
                request,
                staff,
                permission="users:write",
            )
            from apps.users.services import update_role_identity

            update_role_identity(staff, changes)
        refreshed = _query(request, "users:write").get(pk=staff.pk)
        return success(staff_to_dict(refreshed))
    if request.method == "DELETE":
        _assert_can_manage_entire_staff_account(
            request,
            staff,
            permission="users:write",
        )
        deactivate_staff_account(staff)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def staff_credentials_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "users:write")
    staff = _get(request, pk, "users:write")
    _assert_can_manage_entire_staff_account(
        request,
        staff,
        permission="users:write",
    )
    from apps.users.services import issue_role_credentials

    return success(
        issue_role_credentials(
            staff,
            actor=request.user,
            resource_type="org.StaffProfile",
        )
    )
