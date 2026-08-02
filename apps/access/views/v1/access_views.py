"""Access-config endpoints (A-2) — plain Django views over the layered architecture.

Managing this centre's permission overrides is gated at access:read/write, which only
the director holds by default (*:*) — changing who-can-do-what is a high-trust action.
The master wildcard and the `access` resource itself are never overridable (enforced in
the service + a DB CheckConstraint), so the director's authority and control of
permission management are immutable through this mechanism.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.access.dto.access_dto import OverrideDTO
from apps.access.interfaces.services import IAccessService
from apps.access.presenters import (
    account_type_assignment_to_dict,
    account_type_to_dict,
    override_to_dict,
)
from apps.access.services.account_types import (
    account_type_queryset,
    assign_account_type,
    assignment_queryset,
    create_account_type,
    delete_account_type,
    effective_permissions_for_principal,
    get_account_type,
    replace_account_type_permissions,
    revoke_account_type_assignment,
    update_account_type,
)
from apps.access.validation import permission_catalogue, permission_catalogue_metadata
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import Role, _request_overrides, role_effective_permissions
from core.responses import created, error, no_content, paginated, success
from core.scoping import assert_permission_organization_scope

_RESOURCE = "access"


def _method_not_allowed() -> HttpResponse:
    return error(str(_("Method not allowed.")), code="method_not_allowed", status=405)


def _check_organization_permission(request: HttpRequest, permission: str) -> None:
    check_perm(request, permission)
    assert_permission_organization_scope(
        request,
        permission=permission,
        account_kinds={"staff"},
    )


def _reject_unknown(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationException(
            _("Request contains unknown fields."),
            code="validation_error",
            fields={field: [_("Unknown field.")] for field in unknown},
        )


def _permissions_field(body: dict[str, Any], *, required: bool = False) -> list[str]:
    if "permissions" not in body:
        if required:
            raise ValidationException(
                _("Permissions are required."),
                fields={"permissions": [_("This field is required.")]},
            )
        return []
    value = body["permissions"]
    if not isinstance(value, list):
        raise ValidationException(
            _("Permissions must be a list."),
            fields={"permissions": [_("Must be a list of permission codes.")]},
        )
    permissions: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or len(item) > 64 or "\x00" in item:
            raise ValidationException(
                _("Each permission must be a valid permission-code string."),
                fields={"permissions": [_("Invalid permission at index %(index)s.") % {"index": index}]},
            )
        permissions.append(item)
    return permissions


def _required_int(data: dict[str, Any], name: str) -> int:
    value = int_field(data, name, required=True)
    if value is None:  # pragma: no cover - int_field(required=True) raises first
        raise ValidationException(
            _("A required integer is missing."),
            fields={name: [_("This field is required.")]},
        )
    return value


def _service() -> IAccessService:
    return container.resolve(IAccessService)  # type: ignore[type-abstract]


# --- permission overrides (CRUD) -------------------------------------------
@csrf_exempt
@require_auth
def overrides_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        _check_organization_permission(request, f"{_RESOURCE}:read")
        qs = apply_filters(
            request,
            _service().list_overrides(),
            filter_fields=("role", "effect", "permission"),
            ordering_fields=("role", "permission", "created_at"),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([override_to_dict(o) for o in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        _check_organization_permission(request, f"{_RESOURCE}:write")
        body = read_json(request)
        _reject_unknown(body, {"role", "permission", "effect", "note"})
        dto = OverrideDTO(
            role=str_field(body, "role", max_length=32),
            permission=str_field(body, "permission", max_length=64),
            effect=str_field(body, "effect", max_length=6),
            note=str_field(body, "note", max_length=255),
        )
        return created(override_to_dict(_service().create_override(dto, actor=request.user)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def override_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    _check_organization_permission(
        request,
        f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write",
    )
    override = _service().get_override(pk)
    if override is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(override_to_dict(override))
    if request.method in ("PUT", "PATCH"):
        body = read_json(request)
        _reject_unknown(body, {"role", "permission", "effect", "note"})
        return success(override_to_dict(_service().update_override(override, _changes(body))))
    if request.method == "DELETE":
        _service().delete_override(override)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "role" in body:
        changes["role"] = str_field(body, "role", max_length=32)
    if "permission" in body:
        changes["permission"] = str_field(body, "permission", max_length=64)
    if "effect" in body:
        changes["effect"] = str_field(body, "effect", max_length=6)
    if "note" in body:
        changes["note"] = str_field(body, "note", max_length=255)
    return changes


# --- read-only introspection -----------------------------------------------
@csrf_exempt
@require_auth
def access_roles_view(request: HttpRequest) -> HttpResponse:
    """Every role with its EFFECTIVE permissions (defaults + this centre's overrides),
    as {granted, revoked} so a verb carved out of a resource-wildcard is visible."""
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    _check_organization_permission(request, f"{_RESOURCE}:read")
    req: Any = request  # _request_overrides is duck-typed on the request (typed Request upstream)
    overrides = _request_overrides(req)  # one query, shared across all roles
    roles = {role: role_effective_permissions(role, overrides) for role in Role.ALL}
    return success({"roles": roles})


@csrf_exempt
@require_auth
def access_permissions_view(request: HttpRequest) -> HttpResponse:
    """The catalogue of known permission codes a centre can grant/revoke (the union of
    everything the static matrix references)."""
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    _check_organization_permission(request, f"{_RESOURCE}:read")
    return success(
        {
            "permissions": sorted(permission_catalogue()),
            "permission_details": permission_catalogue_metadata(),
        }
    )


# --- canonical account types -----------------------------------------------
@csrf_exempt
@require_auth
def account_types_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        _check_organization_permission(request, f"{_RESOURCE}:read")
        queryset = apply_filters(
            request,
            account_type_queryset(),
            filter_fields=("account_kind", "is_active", "is_system"),
            ordering_fields=("name", "slug", "account_kind", "created_at", "updated_at"),
        )
        items, total, page, size = paginate(request, queryset)
        return paginated(
            [account_type_to_dict(item) for item in items],
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        _check_organization_permission(request, f"{_RESOURCE}:write")
        body = read_json(request)
        _reject_unknown(
            body,
            {"name", "slug", "account_kind", "description", "is_active", "permissions"},
        )
        account_type = create_account_type(
            name=str_field(body, "name", max_length=100),
            slug=str_field(body, "slug", max_length=100),
            account_kind=str_field(body, "account_kind", max_length=16),
            description=str_field(body, "description", max_length=2000),
            is_active=bool_field(body, "is_active", default=True),
            permissions=_permissions_field(body),
            actor=request.user,
            request=request,
        )
        return created(account_type_to_dict(account_type))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def account_type_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    _check_organization_permission(
        request,
        f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write",
    )
    account_type = get_account_type(pk)
    if read:
        return success(account_type_to_dict(account_type))
    if request.method in ("PUT", "PATCH"):
        body = read_json(request)
        _reject_unknown(body, {"name", "slug", "account_kind", "description", "is_active"})
        changes: dict[str, Any] = {}
        for field, max_length in (("name", 100), ("slug", 100), ("account_kind", 16), ("description", 2000)):
            if field in body:
                changes[field] = str_field(body, field, max_length=max_length)
        if "is_active" in body:
            changes["is_active"] = bool_field(body, "is_active")
        return success(
            account_type_to_dict(
                update_account_type(
                    account_type,
                    changes,
                    actor=request.user,
                    request=request,
                )
            )
        )
    if request.method == "DELETE":
        delete_account_type(account_type, actor=request.user, request=request)
        return no_content()
    return _method_not_allowed()


@csrf_exempt
@require_auth
def account_type_permissions_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    _check_organization_permission(
        request,
        f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write",
    )
    account_type = get_account_type(pk)
    if read:
        return success(
            {
                "account_type": account_type.pk,
                "permissions": sorted(row.permission for row in account_type.permission_rows.all()),
            }
        )
    if request.method in ("PUT", "PATCH"):
        body = read_json(request)
        _reject_unknown(body, {"permissions"})
        updated = replace_account_type_permissions(
            account_type,
            _permissions_field(body, required=True),
            actor=request.user,
            request=request,
        )
        return success(account_type_to_dict(updated))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def account_type_assignments_view(
    request: HttpRequest,
    account_type_pk: int | None = None,
) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        queryset = assignment_queryset(request, permission=f"{_RESOURCE}:read")
        if account_type_pk is not None:
            queryset = queryset.filter(account_type_id=account_type_pk)
        principal_kind = request.GET.get("principal_kind", "")
        principal_id_raw = request.GET.get("principal_id")
        if principal_kind or principal_id_raw:
            if not principal_kind or principal_id_raw is None:
                raise ValidationException(
                    _("principal_kind and principal_id must be supplied together."),
                    fields={"principal": [_("Supply both principal filters.")]},
                )
            from apps.access.services.account_types import resolve_principal

            principal_id = _required_int({"principal_id": principal_id_raw}, "principal_id")
            principal = resolve_principal(principal_kind, principal_id)
            principal_queryset = queryset.filter(user_id=principal.user_id)
            if not principal_queryset.exists():
                raise NotFoundException(_("Principal not found."), code="principal_not_found")
            queryset = principal_queryset
        queryset = apply_filters(
            request,
            queryset,
            filter_fields=("account_type", "branch", "department"),
            ordering_fields=("granted_at", "account_type__name", "branch__name"),
        )
        items, total, page, size = paginate(request, queryset)
        return paginated(
            [account_type_assignment_to_dict(item) for item in items],
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        allowed = {"principal_kind", "principal_id", "branch", "department"}
        if account_type_pk is None:
            allowed.add("account_type")
        _reject_unknown(body, allowed)
        resolved_type_id = (
            account_type_pk if account_type_pk is not None else _required_int(body, "account_type")
        )
        principal_id = _required_int(body, "principal_id")
        branch_id = _required_int(body, "branch")
        membership = assign_account_type(
            account_type=get_account_type(resolved_type_id),
            principal_kind=str_field(body, "principal_kind", max_length=16),
            principal_id=principal_id,
            branch_id=branch_id,
            department_id=int_field(body, "department"),
            actor=request.user,
            request=request,
        )
        return created(account_type_assignment_to_dict(membership))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def account_type_assignment_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    check_perm(request, f"{_RESOURCE}:write")
    if request.method != "DELETE":
        return _method_not_allowed()
    membership = assignment_queryset(request, permission=f"{_RESOURCE}:write").filter(pk=pk).first()
    if membership is None:
        raise NotFoundException(_("Assignment not found."), code="assignment_not_found")
    revoke_account_type_assignment(membership, actor=request.user, request=request)
    return no_content()


@csrf_exempt
@require_auth
def account_type_effective_permissions_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return _method_not_allowed()
    _check_organization_permission(request, f"{_RESOURCE}:read")
    principal_kind = request.GET.get("principal_kind", "")
    principal_id = _required_int(
        {"principal_id": request.GET.get("principal_id")},
        "principal_id",
    )
    return success(effective_permissions_for_principal(principal_kind, principal_id))
