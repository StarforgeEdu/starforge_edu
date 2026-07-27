"""Achievement endpoints — plain Django views over the layered architecture.

Custom achievements (F15-2). Staff with achievements:write create + grant; a
teacher-requested GLOBAL achievement is pending until a manager (achievements:approve)
approves it. Reads are ROW-scoped: a director sees the whole centre; a write-holder
sees their own creations + their branch + the active centre-wide catalogue (and, if
they may approve, the pending-global queue); students/parents see the active catalogue
and their own granted wall via `mine`.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.achievements.dto.achievement_dto import CreateAchievementDTO, GrantAchievementDTO
from apps.achievements.interfaces.services import IAchievementService
from apps.achievements.models import Achievement
from apps.achievements.presenters import achievement_grant_to_dict, achievement_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
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
from core.scoping import branch_ids, is_unscoped

_RESOURCE = "achievements"


def _service() -> IAchievementService:
    return container.resolve(IAchievementService)  # type: ignore[type-abstract]


def _scope(request: HttpRequest) -> tuple[bool, bool, bool, set[int]]:
    """(is_unscoped, can_write, can_approve, branch_ids) for the caller."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    overrides = _request_overrides(req)
    is_unscoped = getattr(req.user, "is_superuser", False) or Role.DIRECTOR in roles
    can_write = has_permission_code(roles, f"{_RESOURCE}:write", overrides)
    can_approve = is_unscoped or has_permission_code(roles, f"{_RESOURCE}:approve", overrides)
    branch_ids = {m.branch_id for m in get_role_memberships(req) if m.branch_id}
    return is_unscoped, can_write, can_approve, branch_ids


def _get_visible(request: HttpRequest, pk: int) -> Achievement:
    is_unscoped, can_write, can_approve, branch_ids = _scope(request)
    achievement = _service().get_visible(
        user=request.user,
        is_unscoped=is_unscoped,
        can_write=can_write,
        can_approve=can_approve,
        branch_ids=branch_ids,
        pk=pk,
    )
    if achievement is None:
        raise NotFoundException(code="not_found")  # not in the caller's scope -> 404, no leak
    return achievement


@csrf_exempt
@require_auth
def achievements_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        is_unscoped, can_write, can_approve, branch_ids = _scope(request)
        qs = _service().scoped_list(
            user=request.user,
            is_unscoped=is_unscoped,
            can_write=can_write,
            can_approve=can_approve,
            branch_ids=branch_ids,
        )
        qs = apply_filters(
            request,
            qs,
            filter_fields=("scope", "status", "cohort", "branch"),
            ordering_fields=("created_at", "name"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([achievement_to_dict(a) for a in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def achievement_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    return success(achievement_to_dict(_get_visible(request, pk)))


@csrf_exempt
@require_auth
def achievement_approve_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _decide(request, pk, approve=True)


@csrf_exempt
@require_auth
def achievement_reject_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _decide(request, pk, approve=False)


@csrf_exempt
@require_auth
def achievement_grant_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    achievement = _get_visible(request, pk)
    body = read_json(request)
    student = _service().resolve_student(int_field(body, "student", required=True))  # type: ignore[arg-type]
    if student is None:
        raise ValidationException(
            "Invalid student.", code="validation_error", fields={"student": ["Not found."]}
        )
    # Object-level scope: the recipient must be in the caller's branch (unless the
    # caller is unscoped — director/superuser). Without this a branch-scoped teacher
    # could grant a GLOBAL achievement to another branch's student (cross-branch write)
    # and use the endpoint as a student-pk existence oracle. Mirrors the sales / cards /
    # compliance student-write paths.
    if not is_unscoped(request) and student.branch_id not in branch_ids(request):
        raise PermissionException(
            "You can only grant to a student in your own branch.", code="branch_out_of_scope"
        )
    dto = GrantAchievementDTO(student_id=student.pk, note=str_field(body, "note", max_length=255))
    grant = _service().grant(achievement, dto, granted_by=request.user, student=student)
    return created(achievement_grant_to_dict(grant))


@csrf_exempt
@require_auth
def achievements_mine_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    # A student sees their own wall; a parent sees their guardian-linked children's.
    qs = _service().wall_for(request.user)
    items, total, page, size = paginate(request, qs)
    return paginated([achievement_grant_to_dict(g) for g in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def achievement_grants_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    # Staff-only: who earned an achievement (+ the staff notes) is NOT for a
    # student/parent to enumerate — they only get their own wall via `mine`.
    check_perm(request, f"{_RESOURCE}:write")
    achievement = _get_visible(request, pk)
    qs = _service().grants_of(achievement)
    items, total, page, size = paginate(request, qs)
    return paginated([achievement_grant_to_dict(g) for g in items], total=total, page=page, page_size=size)


# --- helpers ---------------------------------------------------------------
def _decide(request: HttpRequest, pk: int, *, approve: bool) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:approve")
    achievement = _get_visible(request, pk)  # 404 if the approver can't see it (no leak)
    decided = _service().decide(achievement_id=achievement.pk, approve=approve, actor=request.user)
    return success(achievement_to_dict(decided))


def _create(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    # Trim to mirror the old serializer's DRF CharField (trim_whitespace=True,
    # allow_blank=False) so an all-whitespace name is rejected, not stored as junk.
    name = str_field(body, "name", max_length=120).strip()
    if not name:
        raise ValidationException(
            "Name is required.", code="validation_error", fields={"name": ["This field is required."]}
        )
    scope = str_field(body, "scope")
    if scope not in Achievement.Scope.values:
        raise ValidationException(
            "Invalid scope.",
            code="validation_error",
            fields={"scope": [f"Must be one of {', '.join(Achievement.Scope.values)}."]},
        )
    dto = CreateAchievementDTO(
        name=name,
        scope=scope,
        description=str_field(body, "description"),
        emoji=str_field(body, "emoji", max_length=8),  # matches the old serializer's cap
        cohort_id=int_field(body, "cohort"),
    )
    is_unscoped, _can_write, can_approve, branch_ids = _scope(request)
    achievement = _service().create(
        dto, creator=request.user, can_approve=can_approve, is_scoped=not is_unscoped, branch_ids=branch_ids
    )
    return created(achievement_to_dict(achievement))
