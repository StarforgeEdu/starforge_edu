"""Staff-meeting endpoints — plain Django views over the layered architecture.

Scheduling + cancelling are meeting:write; reading, RSVP, and /upcoming are open to
any authenticated user and ROW-scoped: superuser/DIRECTOR see all, a manager sees
their branch's meetings union ones they were invited to, everyone else only their invites.
A non-director scheduler must name a branch in their own scope.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.meetings.dto.meeting_dto import ScheduleMeetingDTO
from apps.meetings.interfaces.services import IMeetingService
from apps.meetings.models import MeetingAttendee
from apps.meetings.presenters import meeting_to_dict
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

_RESOURCE = "meeting"


def _service() -> IMeetingService:
    return container.resolve(IMeetingService)  # type: ignore[type-abstract]


def _scope(request: HttpRequest) -> tuple[bool, bool, set[int]]:
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    is_unscoped = getattr(req.user, "is_superuser", False) or Role.DIRECTOR in roles
    is_manager = has_permission_code(roles, f"{_RESOURCE}:write", _request_overrides(req))
    branch_ids = {m.branch_id for m in get_role_memberships(req) if m.branch_id}
    return is_unscoped, is_manager, branch_ids


def _get_visible(request: HttpRequest, pk: int):
    is_unscoped, is_manager, branch_ids = _scope(request)
    meeting = _service().get_visible(
        user=request.user, is_unscoped=is_unscoped, is_manager=is_manager, branch_ids=branch_ids, pk=pk
    )
    if meeting is None:
        raise NotFoundException(code="not_found")  # not in the caller's scope -> 404, no leak
    return meeting


@csrf_exempt
@require_auth
def meetings_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        is_unscoped, is_manager, branch_ids = _scope(request)
        qs = _service().scoped_list(
            user=request.user, is_unscoped=is_unscoped, is_manager=is_manager, branch_ids=branch_ids
        )
        qs = apply_filters(
            request, qs, filter_fields=("status", "branch"),
            ordering_fields=("starts_at",), default_ordering="starts_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([meeting_to_dict(m) for m in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def meeting_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return success(meeting_to_dict(_get_visible(request, pk)))


@csrf_exempt
@require_auth
def meeting_cancel_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    meeting = _get_visible(request, pk)
    return success(meeting_to_dict(_service().cancel(meeting, actor=request.user)))


@csrf_exempt
@require_auth
def meeting_respond_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    meeting = _get_visible(request, pk)  # invitees RSVP without a write perm; row-scoped
    body = read_json(request)
    response = str_field(body, "response")
    if response not in (MeetingAttendee.Response.ACCEPTED, MeetingAttendee.Response.DECLINED):
        raise ValidationException(
            "Invalid response.", code="validation_error", fields={"response": ["Must be accepted or declined."]}
        )
    _service().respond(meeting, user=request.user, response=response)
    return success(meeting_to_dict(_get_visible(request, pk)))


@csrf_exempt
@require_auth
def meetings_upcoming_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return success([meeting_to_dict(m) for m in _service().upcoming_for(request.user)])


# --- helpers ---------------------------------------------------------------
def _create(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    title = str_field(body, "title", max_length=200)
    if not title:
        raise ValidationException(
            "Title is required.", code="validation_error", fields={"title": ["This field is required."]}
        )
    # Parse + validate the body (400s) BEFORE the branch-scope check (403) — matches
    # the old serializer-before-perform_create ordering.
    dto = ScheduleMeetingDTO(
        title=title,
        agenda=str_field(body, "agenda"),
        location=str_field(body, "location", max_length=200),
        starts_at=_datetime(body, "starts_at"),
        ends_at=_datetime(body, "ends_at"),
        branch_id=int_field(body, "branch"),
        attendee_ids=_int_list(body, "attendees"),
    )
    _service().resolve_branch(dto.branch_id)  # 400 invalid_branch if archived/missing
    is_unscoped, _is_manager, branch_ids = _scope(request)
    _assert_branch_in_scope(is_unscoped, dto.branch_id, branch_ids)  # 403 branch_required / out_of_scope
    return created(meeting_to_dict(_service().schedule(dto, created_by=request.user)))


def _assert_branch_in_scope(is_unscoped: bool, branch_id: int | None, branch_ids: set[int]) -> None:
    if is_unscoped:
        return
    if branch_id is None:
        raise PermissionException("Choose a branch for the meeting.", code="branch_required")
    if branch_id not in branch_ids:
        raise PermissionException(
            "You can only schedule a meeting for your own branch.", code="branch_out_of_scope"
        )


def _datetime(body: dict[str, Any], name: str):
    raw = body.get(name)
    if not raw or not isinstance(raw, str):
        raise ValidationException(
            f"{name} is required.", code="validation_error", fields={name: ["Required (ISO 8601)."]}
        )
    try:
        # parse_datetime RAISES ValueError for a well-formed-but-invalid value
        # (e.g. 2026-02-30T10:00) — not just returns None — so catch it: bad input
        # must be a clean 400, never a 500.
        dt = parse_datetime(raw)
    except ValueError:
        dt = None
    if dt is None:
        raise ValidationException(
            "Invalid datetime.", code="validation_error", fields={name: ["Must be an ISO 8601 datetime."]}
        )
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _int_list(body: dict[str, Any], name: str) -> list[int]:
    raw = body.get(name, [])
    if not isinstance(raw, list):
        raise ValidationException(
            "Invalid list.", code="validation_error", fields={name: ["Must be a list of ids."]}
        )
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ValidationException(
                "Invalid id.", code="validation_error", fields={name: ["Each item must be an id."]}
            )
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            raise ValidationException(
                "Invalid id.", code="validation_error", fields={name: ["Each item must be an integer id."]}
            ) from None
    return out
