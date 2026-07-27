"""Room endpoints — branch-scoped CRUD (object_scope='branch')."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.org.dto.org_dto import RoomCreateDTO
from apps.org.interfaces.services import IRoomService
from apps.org.presenters import room_to_dict
from apps.org.views.v1._shared import require_present
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.responses import created, error, no_content, paginated, success
from core.scoping import assert_branch_id_in_scope, assert_in_branch_scope, scope_to_branches

_RESOURCE = "org"
_FILTERS = ("branch", "is_active")
_SEARCH = ("name",)
_ORDERING = ("name", "created_at")


def _service() -> IRoomService:
    return container.resolve(IRoomService)  # type: ignore[type-abstract]


@csrf_exempt
@require_auth
def rooms_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = scope_to_branches(request, _service().list())
        qs = apply_filters(
            request, qs, filter_fields=_FILTERS, search_fields=_SEARCH,
            ordering_fields=_ORDERING, default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([room_to_dict(r) for r in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        branch_id = int_field(body, "branch", required=True)
        assert_branch_id_in_scope(request, branch_id)
        name = str_field(body, "name")
        require_present({"name": name})
        dto = RoomCreateDTO(
            branch_id=branch_id,  # type: ignore[arg-type]
            name=name,
            capacity=int_field(body, "capacity", default=0),  # type: ignore[arg-type]
            equipment=_list_field(body, "equipment"),
            is_active=bool_field(body, "is_active", default=True),
            notes=str_field(body, "notes"),
        )
        return created(room_to_dict(_service().create(dto)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def room_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    room = _service().get(pk)
    if room is None:
        raise NotFoundException(code="not_found")
    assert_in_branch_scope(request, room)
    if read:
        return success(room_to_dict(room))
    if request.method in ("PUT", "PATCH"):
        changes = _changes(read_json(request))
        if "branch" in changes:  # reassignment must land in a branch the caller can reach
            assert_branch_id_in_scope(request, changes["branch"])
        return success(room_to_dict(_service().update(room, changes)))
    if request.method == "DELETE":
        _service().delete(room)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "branch" in body:
        changes["branch"] = int_field(body, "branch", required=True)
    if "name" in body:
        changes["name"] = str_field(body, "name")
    if "capacity" in body:
        changes["capacity"] = int_field(body, "capacity", default=0)
    if "equipment" in body:
        changes["equipment"] = _list_field(body, "equipment")
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active", default=True)
    if "notes" in body:
        changes["notes"] = str_field(body, "notes")
    return changes


def _list_field(body: dict[str, Any], name: str) -> list:
    raw = body.get(name, [])
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValidationException(
            "Invalid list.", code="validation_error", fields={name: ["Must be a list."]}
        )
    return raw
