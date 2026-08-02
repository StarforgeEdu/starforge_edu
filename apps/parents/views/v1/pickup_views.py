"""Pickup-authorization endpoints — layered plain views. Full CRUD; same role scoping."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.parents.dto.parent_dto import PickupCreateDTO
from apps.parents.interfaces.services import IPickupService
from apps.parents.presenters import pickup_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, no_content, paginated, success, validation_error

_RESOURCE = "parents"
_FILTERS = ("student", "is_active")


def _service() -> IPickupService:
    return container.resolve(IPickupService)  # type: ignore[type-abstract]


@csrf_exempt
@require_auth
def pickups_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = _service().scoped_list(
            user=request.user,
            roles=get_user_roles(request),
            permission=f"{_RESOURCE}:read",
        )
        qs = apply_filters(
            request,
            qs,
            filter_fields=_FILTERS,
            ordering_fields=("created_at",),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([pickup_to_dict(p) for p in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        _reject_unknown_fields(
            body,
            allowed=frozenset({"student", "full_name", "phone", "relationship", "is_active"}),
        )
        full_name = str_field(body, "full_name", max_length=200)
        phone = str_field(body, "phone", max_length=32)
        errors = {}
        if not full_name:
            errors["full_name"] = ["This field is required."]
        if not phone:
            errors["phone"] = ["This field is required."]
        if errors:
            return validation_error(errors)
        dto = PickupCreateDTO(
            student_id=int_field(body, "student", required=True),  # type: ignore[arg-type]
            full_name=full_name,
            phone=phone,
            relationship=str_field(body, "relationship", max_length=32),
            is_active=bool_field(body, "is_active", default=True),
        )
        return created(
            pickup_to_dict(_service().create(dto, user=request.user, roles=get_user_roles(request)))
        )
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def pickup_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    if not read:
        return _pickup_detail_write(request, pk)
    check_perm(request, f"{_RESOURCE}:read")
    pickup = _service().get(
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:read",
        pk=pk,
    )
    if pickup is None:
        raise NotFoundException(code="not_found")
    return success(pickup_to_dict(pickup))


@transaction.atomic
def _pickup_detail_write(request: HttpRequest, pk: int) -> HttpResponse:
    check_perm(request, f"{_RESOURCE}:write")
    pickup = _service().get(
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:write",
        pk=pk,
    )
    if pickup is None:
        raise NotFoundException(code="not_found")
    if request.method in ("PUT", "PATCH"):
        # PUT and PATCH are both partial here — the deliberate off-DRF convention.
        body = read_json(request)
        _reject_unknown_fields(
            body,
            allowed=frozenset({"full_name", "phone", "relationship", "is_active"}),
        )
        changes = _changes(body)
        errors: dict[str, list[str]] = {}
        for field in ("full_name", "phone"):
            if field in changes and not changes[field]:
                errors[field] = [str(_("This field may not be blank."))]
        if errors:
            return validation_error(errors)
        return success(
            pickup_to_dict(
                _service().update(
                    pickup,
                    changes,
                    user=request.user,
                    roles=get_user_roles(request),
                    actor=request.user,
                )
            )
        )
    if request.method == "DELETE":
        _reject_unknown_fields(read_json(request), allowed=frozenset())
        _service().deactivate(
            pickup,
            user=request.user,
            roles=get_user_roles(request),
            actor=request.user,
        )
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "full_name" in body:
        changes["full_name"] = str_field(body, "full_name", max_length=200)
    if "phone" in body:
        changes["phone"] = str_field(body, "phone", max_length=32)
    if "relationship" in body:
        changes["relationship"] = str_field(body, "relationship", max_length=32)
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active", default=True)
    return changes


def _reject_unknown_fields(body: dict[str, Any], *, allowed: frozenset[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        from core.exceptions import ValidationException

        raise ValidationException(
            _("Unsupported pickup-authorization field."),
            code="validation_error",
            fields={field: [_("This field is not supported.")] for field in unknown},
        )
