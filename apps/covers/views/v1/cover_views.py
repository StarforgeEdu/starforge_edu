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
    _request_overrides,
    get_user_roles,
    has_permission_code,
)
from core.responses import created, error, paginated, success
from core.scoping import (
    assert_permission_membership_scope,
    is_permission_unscoped,
    permission_membership_branch_ids,
)

_RESOURCE = "cover"
_FILTER_FIELDS = ("status", "pool", "branch", "lesson")


def _service() -> ICoverService:
    return container.resolve(ICoverService)  # type: ignore[type-abstract]


def _scope(request: HttpRequest, *, permission: str) -> tuple[bool, bool, set[int], set[int]]:
    """Permission-paired manager and teacher branch scopes for the caller."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    unscoped = is_permission_unscoped(req, permission=permission)
    is_manager = has_permission_code(roles, f"{_RESOURCE}:approve", _request_overrides(req))
    manager_branch_ids = permission_membership_branch_ids(roles=roles, permission=f"{_RESOURCE}:approve")
    teacher_branch_ids = permission_membership_branch_ids(roles=roles, permission=f"{_RESOURCE}:write")
    return unscoped, is_manager, manager_branch_ids, teacher_branch_ids


def _get_visible(
    request: HttpRequest,
    pk: int,
    *,
    permission: str = "cover:read",
) -> CoverRequest:
    unscoped, is_manager, manager_branch_ids, teacher_branch_ids = _scope(
        request,
        permission=permission,
    )
    cover = _service().get_visible(
        user=request.user,
        is_unscoped=unscoped,
        is_manager=is_manager,
        manager_branch_ids=manager_branch_ids,
        teacher_branch_ids=teacher_branch_ids,
        pk=pk,
    )
    if cover is None:
        raise NotFoundException(code="not_found")  # not in the caller's scope -> 404, no leak
    return cover


@csrf_exempt
@require_auth
def covers_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:read")
        unscoped, is_manager, manager_branch_ids, teacher_branch_ids = _scope(
            request,
            permission=f"{_RESOURCE}:read",
        )
        qs = _service().scoped_list(
            user=request.user,
            is_unscoped=unscoped,
            is_manager=is_manager,
            manager_branch_ids=manager_branch_ids,
            teacher_branch_ids=teacher_branch_ids,
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
        unscoped, _is_manager, _manager_branch_ids, teacher_branch_ids = _scope(
            request,
            permission=f"{_RESOURCE}:write",
        )
        return created(
            cover_to_dict(
                _service().create(
                    dto,
                    requester=request.user,
                    is_unscoped=unscoped,
                    branch_ids=teacher_branch_ids,
                )
            )
        )
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
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    # The claimable cover board (F18-2): open requests a manager has opened to the pool,
    # scoped to the caller's branch(es) — what a teacher can claim right now.
    unscoped, is_manager, manager_branch_ids, teacher_branch_ids = _scope(
        request,
        permission=f"{_RESOURCE}:read",
    )
    qs = _service().scoped_list(
        user=request.user,
        is_unscoped=unscoped,
        is_manager=is_manager,
        manager_branch_ids=manager_branch_ids,
        teacher_branch_ids=teacher_branch_ids,
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
    cover = _get_visible(request, pk, permission=f"{_RESOURCE}:approve")
    _assert_cover_permission_scope(request, cover, f"{_RESOURCE}:approve")
    cover_teacher_id = int_field(read_json(request), "cover_teacher", required=True)
    result = _service().assign(cover_id=cover.pk, cover_teacher_id=cover_teacher_id, actor=request.user)  # type: ignore[arg-type]
    return success(cover_to_dict(result))


@csrf_exempt
@require_auth
def cover_open_pool_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:approve")
    cover = _get_visible(request, pk, permission=f"{_RESOURCE}:approve")
    _assert_cover_permission_scope(request, cover, f"{_RESOURCE}:approve")
    return success(cover_to_dict(_service().open_pool(cover_id=cover.pk, actor=request.user)))


@csrf_exempt
@require_auth
def cover_claim_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    cover = _get_visible(request, pk, permission=f"{_RESOURCE}:write")
    _assert_cover_permission_scope(request, cover, f"{_RESOURCE}:write")
    return success(
        cover_to_dict(_service().claim(cover_id=cover.pk, claimer_user=request.user, actor=request.user))
    )


@csrf_exempt
@require_auth
def cover_cancel_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    cover = _get_visible(request, pk, permission=f"{_RESOURCE}:write")
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
    cover = _get_visible(request, pk, permission=f"{_RESOURCE}:approve")
    _assert_cover_permission_scope(request, cover, f"{_RESOURCE}:approve")
    return success(cover_to_dict(_service().reject(cover_id=cover.pk, actor=request.user)))


def _assert_cover_permission_scope(request: HttpRequest, cover: CoverRequest, permission: str) -> None:
    assert_permission_membership_scope(
        request,
        permission=permission,
        branch_id=cover.branch_id,
        enforce_department=False,
    )
