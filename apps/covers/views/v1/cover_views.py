"""Lesson-cover endpoints — plain Django views over the layered architecture.

A teacher (cover:write) requests cover for their own lesson; a manager (cover:approve)
assigns a cover teacher or opens it to the branch pool; a teacher claims a pooled
request. Approval reassigns the lesson. Reads are ROW-scoped (director all; manager =
their branch; teacher = own + claimable pool + assigned-to-them).
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.covers.dto.cover_dto import CreateCoverDTO
from apps.covers.interfaces.services import ICoverService
from apps.covers.models import CoverRequest
from apps.covers.presenters import cover_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException
from core.http import int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import (
    Role,
    _request_overrides,
    get_role_memberships,
    get_user_roles,
    has_permission_code,
)
from core.responses import created, error, paginated, success

_RESOURCE = "cover"
_FILTER_FIELDS = ("status", "pool", "branch", "lesson")


def _service() -> ICoverService:
    return container.resolve(ICoverService)  # type: ignore[type-abstract]


def _scope(request: HttpRequest) -> tuple[bool, bool, set[int]]:
    """(is_unscoped, is_manager, branch_ids) for the caller."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    is_unscoped = getattr(req.user, "is_superuser", False) or Role.DIRECTOR in roles
    is_manager = has_permission_code(roles, f"{_RESOURCE}:approve", _request_overrides(req))
    branch_ids = {m.branch_id for m in get_role_memberships(req) if m.branch_id}
    return is_unscoped, is_manager, branch_ids


def _get_visible(request: HttpRequest, pk: int) -> CoverRequest:
    is_unscoped, is_manager, branch_ids = _scope(request)
    cover = _service().get_visible(
        user=request.user, is_unscoped=is_unscoped, is_manager=is_manager, branch_ids=branch_ids, pk=pk
    )
    if cover is None:
        raise NotFoundException(code="not_found")  # not in the caller's scope -> 404, no leak
    return cover


@csrf_exempt
@require_auth
def covers_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        is_unscoped, is_manager, branch_ids = _scope(request)
        qs = _service().scoped_list(
            user=request.user, is_unscoped=is_unscoped, is_manager=is_manager, branch_ids=branch_ids
        )
        qs = apply_filters(
            request,
            qs,
            filter_fields=_FILTER_FIELDS,
            ordering_fields=("created_at",),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([cover_to_dict(c) for c in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        dto = CreateCoverDTO(
            lesson_id=int_field(body, "lesson", required=True),  # type: ignore[arg-type]
            reason=str_field(body, "reason", max_length=255),
        )
        return created(cover_to_dict(_service().create(dto, requester=request.user)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def cover_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    return success(cover_to_dict(_get_visible(request, pk)))


@csrf_exempt
@require_auth
def cover_pool_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    # The claimable cover board (F18-2): open requests a manager has opened to the pool,
    # scoped to the caller's branch(es) — what a teacher can claim right now.
    is_unscoped, is_manager, branch_ids = _scope(request)
    qs = _service().scoped_list(
        user=request.user, is_unscoped=is_unscoped, is_manager=is_manager, branch_ids=branch_ids
    )
    qs = apply_filters(
        request,
        qs,
        filter_fields=_FILTER_FIELDS,
        ordering_fields=("created_at",),
        default_ordering="-created_at",
    )
    qs = qs.filter(pool=True, status=CoverRequest.Status.OPEN)
    items, total, page, size = paginate(request, qs)
    return paginated([cover_to_dict(c) for c in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def cover_assign_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:approve")
    cover = _get_visible(request, pk)
    cover_teacher_id = int_field(read_json(request), "cover_teacher", required=True)
    result = _service().assign(cover_id=cover.pk, cover_teacher_id=cover_teacher_id, actor=request.user)  # type: ignore[arg-type]
    return success(cover_to_dict(result))


@csrf_exempt
@require_auth
def cover_open_pool_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:approve")
    cover = _get_visible(request, pk)
    return success(cover_to_dict(_service().open_pool(cover_id=cover.pk, actor=request.user)))


@csrf_exempt
@require_auth
def cover_claim_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    cover = _get_visible(request, pk)
    return success(
        cover_to_dict(_service().claim(cover_id=cover.pk, claimer_user=request.user, actor=request.user))
    )


@csrf_exempt
@require_auth
def cover_cancel_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    cover = _get_visible(request, pk)
    # Only the requester may withdraw their own request.
    if not getattr(request.user, "is_superuser", False) and cover.requester_id != request.user.id:
        raise PermissionException(_("You can only cancel your own request."), code="not_requester")
    return success(cover_to_dict(_service().cancel(cover_id=cover.pk, actor=request.user)))


@csrf_exempt
@require_auth
def cover_reject_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:approve")
    cover = _get_visible(request, pk)
    return success(cover_to_dict(_service().reject(cover_id=cover.pk, actor=request.user)))
