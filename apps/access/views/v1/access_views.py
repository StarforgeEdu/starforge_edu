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
from django.views.decorators.csrf import csrf_exempt

from apps.access.dto.access_dto import OverrideDTO
from apps.access.interfaces.services import IAccessService
from apps.access.presenters import override_to_dict
from apps.access.validation import permission_catalogue
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException
from core.http import read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import Role, _request_overrides, role_effective_permissions
from core.responses import created, error, no_content, paginated, success

_RESOURCE = "access"


def _service() -> IAccessService:
    return container.resolve(IAccessService)  # type: ignore[type-abstract]


# --- permission overrides (CRUD) -------------------------------------------
@csrf_exempt
@require_auth
def overrides_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = apply_filters(
            request,
            _service().list_overrides(),
            filter_fields=("role", "effect", "permission"),
            ordering_fields=("role", "permission", "created_at"),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([override_to_dict(o) for o in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
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
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    override = _service().get_override(pk)
    if override is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(override_to_dict(override))
    if request.method in ("PUT", "PATCH"):
        return success(override_to_dict(_service().update_override(override, _changes(read_json(request)))))
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
    check_perm(request, f"{_RESOURCE}:read")
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
    check_perm(request, f"{_RESOURCE}:read")
    return success({"permissions": sorted(permission_catalogue())})
