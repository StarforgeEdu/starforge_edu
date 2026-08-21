"""Reward endpoints — plain Django views over the layered architecture.

RewardType is an unscoped catalog (rewards:read to view, rewards:write to manage;
no DELETE — types are retired via is_active). RewardGrant is role-scoped: a manager
(rewards:write) lists/grants and sees every grant; a staff member (rewards:read)
only sees grants they received or made, and their own wall via `mine`.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.rewards.dto.reward_dto import GrantRewardDTO, RewardTypeCreateDTO
from apps.rewards.interfaces.services import IRewardGrantService, IRewardTypeService
from apps.rewards.presenters import reward_grant_to_dict, reward_type_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, decimal_field, int_field, read_json, trimmed_str_field
from core.listing import apply_filters, paginate
from core.permissions import _request_overrides, get_user_roles, has_permission_code
from core.responses import created, error, paginated, success

_RESOURCE = "rewards"


def _type_service() -> IRewardTypeService:
    return container.resolve(IRewardTypeService)  # type: ignore[type-abstract]


def _grant_service() -> IRewardGrantService:
    return container.resolve(IRewardGrantService)  # type: ignore[type-abstract]


def _is_manager(request: HttpRequest) -> bool:
    req: Any = request  # the permission helpers are duck-typed on .user (typed Request upstream)
    if getattr(req.user, "is_superuser", False):
        return True
    return has_permission_code(get_user_roles(req), f"{_RESOURCE}:write", _request_overrides(req))


# --- reward types ----------------------------------------------------------
@csrf_exempt
@require_auth
def reward_types_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:read")
        qs = apply_filters(
            request,
            _type_service().list(),
            filter_fields=("is_cash", "is_active"),
            search_fields=("name",),
            ordering_fields=("name", "created_at"),
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([reward_type_to_dict(t) for t in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        name = trimmed_str_field(body, "name", max_length=120, required=True)
        if not name:
            raise ValidationException(
                "Name is required.", code="validation_error", fields={"name": ["This field is required."]}
            )
        dto = RewardTypeCreateDTO(
            name=name,
            is_cash=bool_field(body, "is_cash"),
            default_amount_uzs=decimal_field(body, "default_amount_uzs", max_digits=18),
            description=trimmed_str_field(body, "description"),
            is_active=bool_field(body, "is_active", default=True),
        )
        return created(reward_type_to_dict(_type_service().create(dto, creator=request.user)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def reward_type_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    reward_type = _type_service().get(pk)
    if reward_type is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(reward_type_to_dict(reward_type))
    if request.method in ("PUT", "PATCH"):
        body = read_json(request)
        if request.method == "PUT" and "name" not in body:
            raise ValidationException(
                "Name is required for a full update.",
                code="validation_error",
                fields={"name": ["Required."]},
            )
        return success(reward_type_to_dict(_type_service().update(reward_type, _type_changes(body))))
    # No DELETE — a type with grants is PROTECTed and kept for the audit trail.
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _type_changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "name" in body:
        name = trimmed_str_field(body, "name", max_length=120)
        if not name:  # the create path rejects a blank name; keep update symmetric
            raise ValidationException(
                "Name may not be blank.", code="validation_error", fields={"name": ["May not be blank."]}
            )
        changes["name"] = name
    if "is_cash" in body:
        changes["is_cash"] = bool_field(body, "is_cash")
    if "default_amount_uzs" in body:
        changes["default_amount_uzs"] = decimal_field(body, "default_amount_uzs", max_digits=18)
    if "description" in body:
        changes["description"] = trimmed_str_field(body, "description")
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active", default=True)
    return changes


# --- reward grants ---------------------------------------------------------
@csrf_exempt
@require_auth
def reward_grants_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:write")  # listing ALL grants is a manager action
        qs = apply_filters(
            request,
            _grant_service().list_all(),
            filter_fields=("reward_type", "recipient"),
            ordering_fields=("granted_at",),
            default_ordering="-granted_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([reward_grant_to_dict(g) for g in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        amount = decimal_field(body, "amount_uzs", max_digits=18)
        if amount is not None and amount < 0:  # old serializer had min_value=0 for all grants
            raise ValidationException(
                "Amount must be non-negative.",
                code="validation_error",
                fields={"amount_uzs": ["Must be non-negative."]},
            )
        dto = GrantRewardDTO(
            reward_type_id=int_field(body, "reward_type", required=True),  # type: ignore[arg-type]
            recipient_id=int_field(body, "recipient", required=True),  # type: ignore[arg-type]
            amount_uzs=amount,
            reason=trimmed_str_field(body, "reason", max_length=255),
        )
        return created(reward_grant_to_dict(_grant_service().grant(dto, granted_by=request.user)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def reward_grant_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    grant = _grant_service().get_visible(user=request.user, is_manager=_is_manager(request), pk=pk)
    if grant is None:
        raise NotFoundException(code="not_found")  # out-of-scope grant -> 404, no leak
    return success(reward_grant_to_dict(grant))


@csrf_exempt
@require_auth
def reward_grants_mine_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    qs = apply_filters(
        request,
        _grant_service().received_by(request.user),
        ordering_fields=("granted_at",),
        default_ordering="-granted_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([reward_grant_to_dict(g) for g in items], total=total, page=page, page_size=size)
