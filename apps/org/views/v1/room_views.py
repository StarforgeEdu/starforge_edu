"""Room endpoints — branch-scoped CRUD (object_scope='branch')."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.org.dto.org_dto import RoomCreateDTO
from apps.org.interfaces.services import IRoomService
from apps.org.presenters import room_to_dict
from apps.org.views.v1._shared import require_present
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import (
    bool_field,
    int_field,
    read_json,
    reject_unknown_fields,
    trimmed_str_field,
)
from core.listing import apply_filters, paginate
from core.responses import created, error, no_content, paginated, success
from core.scoping import assert_permission_membership_scope, scope_to_permission_memberships

_RESOURCE = "org"
_FILTERS = ("branch", "is_active")
_SEARCH = ("name",)
_ORDERING = ("name", "created_at")
_CREATE_FIELDS = frozenset({"branch", "name", "capacity", "equipment", "is_active", "notes"})
# Branch identity is immutable for a room: changing it would silently rewrite
# schedule/cohort attribution. A future move workflow must reconcile references.
_UPDATE_FIELDS = frozenset({"name", "capacity", "equipment", "is_active", "notes"})
_MAX_EQUIPMENT_ITEMS = 64
_MAX_EQUIPMENT_ITEM_LENGTH = 100


def _service() -> IRoomService:
    return container.resolve(IRoomService)  # type: ignore[type-abstract]


def _query(request: HttpRequest, permission: str):
    return scope_to_permission_memberships(
        request,
        _service().list(),
        permission=permission,
        branch_field="branch_id",
    )


def _get(
    request: HttpRequest,
    pk: int,
    *,
    permission: str,
    for_update: bool = False,
):
    queryset = _query(request, permission)
    if for_update:
        queryset = queryset.select_for_update(of=("self",))
    room = queryset.filter(pk=pk).first()
    if room is None:
        raise NotFoundException(code="not_found")
    return room


@csrf_exempt
@require_auth
def rooms_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        permission = f"{_RESOURCE}:read"
        check_perm(request, permission)
        qs = _query(request, permission)
        qs = apply_filters(
            request,
            qs,
            filter_fields=_FILTERS,
            search_fields=_SEARCH,
            ordering_fields=_ORDERING,
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([room_to_dict(r) for r in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        permission = f"{_RESOURCE}:write"
        check_perm(request, permission)
        body = read_json(request)
        reject_unknown_fields(body, allowed=_CREATE_FIELDS)
        branch_id = int_field(body, "branch", required=True, min_value=1)
        # Rooms have branch scope only. A department-only grant cannot safely
        # mutate a shared physical resource.
        assert_permission_membership_scope(
            request,
            permission=permission,
            branch_id=branch_id,
            department_id=None,
            enforce_department=True,
        )
        name = trimmed_str_field(body, "name", required=True, max_length=100)
        require_present({"name": name})
        dto = RoomCreateDTO(
            branch_id=branch_id,  # type: ignore[arg-type]
            name=name,
            capacity=int_field(body, "capacity", default=0, min_value=0, max_value=32_767),  # type: ignore[arg-type]
            equipment=_list_field(body, "equipment"),
            is_active=bool_field(body, "is_active", default=True),
            notes=trimmed_str_field(body, "notes", max_length=4_000),
        )
        return created(room_to_dict(_service().create(dto)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def room_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    permission = f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write"
    check_perm(request, permission)
    if read:
        return success(room_to_dict(_get(request, pk, permission=permission)))
    if request.method in ("PUT", "PATCH", "DELETE"):
        with transaction.atomic():
            room = _get(request, pk, permission=permission, for_update=True)
            # Fail closed when the permission exists only at department scope;
            # there is no department attribute with which to narrow a room.
            assert_permission_membership_scope(
                request,
                permission=permission,
                branch_id=room.branch_id,
                department_id=None,
                enforce_department=True,
            )
            if request.method in ("PUT", "PATCH"):
                body = read_json(request)
                reject_unknown_fields(body, allowed=_UPDATE_FIELDS)
                return success(room_to_dict(_service().update(room, _changes(body))))
            _service().delete(room)
            return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "name" in body:
        changes["name"] = trimmed_str_field(body, "name", required=True, max_length=100)
        require_present({"name": changes["name"]})
    if "capacity" in body:
        changes["capacity"] = int_field(
            body,
            "capacity",
            required=True,
            min_value=0,
            max_value=32_767,
        )
    if "equipment" in body:
        changes["equipment"] = _list_field(body, "equipment")
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active", default=True)
    if "notes" in body:
        changes["notes"] = trimmed_str_field(body, "notes", max_length=4_000)
    return changes


def _list_field(body: dict[str, Any], name: str) -> list[str]:
    raw = body.get(name, [])
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValidationException(
            "Invalid list.", code="validation_error", fields={name: ["Must be a list."]}
        )
    if len(raw) > _MAX_EQUIPMENT_ITEMS:
        raise ValidationException(
            "Invalid equipment list.",
            code="validation_error",
            fields={name: [f"At most {_MAX_EQUIPMENT_ITEMS} items are allowed."]},
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValidationException(
                "Invalid equipment list.",
                code="validation_error",
                fields={name: [f"Item {index} must be a non-empty string."]},
            )
        item = item.strip()
        if "\x00" in item or len(item) > _MAX_EQUIPMENT_ITEM_LENGTH:
            raise ValidationException(
                "Invalid equipment list.",
                code="validation_error",
                fields={name: [f"Item {index} is invalid or too long."]},
            )
        if item in seen:
            raise ValidationException(
                "Invalid equipment list.",
                code="validation_error",
                fields={name: ["Duplicate items are not allowed."]},
            )
        seen.add(item)
        normalized.append(item)
    return normalized
