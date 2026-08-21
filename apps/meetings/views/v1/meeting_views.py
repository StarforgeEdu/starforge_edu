"""Staff-meeting endpoints — plain Django views over the layered architecture.

Scheduling + cancelling are meeting:write; reading, RSVP, and /upcoming are open to
any authenticated user and ROW-scoped: superuser/DIRECTOR see all, a manager sees
their branch's meetings union ones they were invited to, everyone else only their invites.
A non-director scheduler must name a branch in their own scope.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.meetings.dto.meeting_dto import MeetingPrincipalTarget, ScheduleMeetingDTO
from apps.meetings.interfaces.services import IMeetingService
from apps.meetings.models import MeetingAttendee, StaffMeeting
from apps.meetings.openapi_contracts import (
    MEETING_CANCEL_CONTRACT,
    MEETING_DETAIL_CONTRACTS,
    MEETING_RESPOND_CONTRACT,
    MEETING_UPCOMING_CONTRACTS,
    MEETINGS_COLLECTION_CONTRACTS,
)
from apps.meetings.presenters import meeting_to_dict
from core.api_auth import check_perm, deny_read_only_token, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import int_field, read_json, str_field
from core.listing import apply_filters, paginate, positive_int_filter, validate_pagination_filters
from core.openapi_contracts import openapi_contract
from core.permissions import _request_overrides, get_user_roles, has_permission_code
from core.responses import created, error, paginated, success
from core.role_principals import (
    STAFF_PRINCIPAL_KINDS,
    RolePrincipal,
    request_role_principal,
)
from core.scoping import is_permission_unscoped, permission_membership_scopes

_RESOURCE = "meeting"
MAX_MEETING_AGENDA_CHARS = 20_000
MAX_MEETING_ATTENDEES = 200
_CREATE_FIELDS = frozenset(
    {"title", "agenda", "location", "starts_at", "ends_at", "branch", "attendees", "invitees"}
)


class _Scope(NamedTuple):
    is_unscoped: bool
    is_manager: bool
    branch_ids: set[int]
    principal: RolePrincipal

    def manages(self, meeting: StaffMeeting) -> bool:
        return self.is_unscoped or (meeting.branch_id is not None and meeting.branch_id in self.branch_ids)


def _service() -> IMeetingService:
    return container.resolve(IMeetingService)  # type: ignore[type-abstract]


def _request_principal(request: HttpRequest) -> RolePrincipal:
    cached = getattr(request, "_meeting_role_principal", None)
    if isinstance(cached, RolePrincipal):
        return cached
    principal = request_role_principal(
        request,
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        error_code="meeting_principal_unavailable",
    )
    request._meeting_role_principal = principal  # type: ignore[attr-defined]
    return principal


def _scope(request: HttpRequest) -> _Scope:
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    superuser = bool(getattr(req.user, "is_superuser", False))
    scopes = permission_membership_scopes(
        roles=roles,
        permission=f"{_RESOURCE}:write",
        account_kinds=STAFF_PRINCIPAL_KINDS,
    )
    return _Scope(
        is_unscoped=superuser
        or is_permission_unscoped(
            req,
            permission=f"{_RESOURCE}:write",
            account_kinds=STAFF_PRINCIPAL_KINDS,
        ),
        is_manager=superuser or has_permission_code(roles, f"{_RESOURCE}:write", _request_overrides(req)),
        # StaffMeeting has no department column. A department-only grant cannot
        # safely become branch-wide meeting authority.
        branch_ids={scope.branch_id for scope in scopes if scope.department_id is None},
        principal=_request_principal(request),
    )


def _present(request: HttpRequest, meeting: StaffMeeting) -> dict[str, Any]:
    scope = _scope(request)
    return meeting_to_dict(
        meeting,
        include_all_attendees=scope.manages(meeting),
        principal_kind=scope.principal.kind,
        principal_id=scope.principal.principal_id,
    )


def _get_visible(request: HttpRequest, pk: int):
    scope = _scope(request)
    meeting = _service().get_visible(
        is_unscoped=scope.is_unscoped,
        is_manager=scope.is_manager,
        branch_ids=scope.branch_ids,
        principal_kind=scope.principal.kind,
        principal_id=scope.principal.principal_id,
        pk=pk,
    )
    if meeting is None:
        raise NotFoundException(code="not_found")  # not in the caller's scope -> 404, no leak
    return meeting


@openapi_contract(
    path="/api/v1/meetings/",
    operations=MEETINGS_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def meetings_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        scope = _scope(request)
        qs = _service().scoped_list(
            is_unscoped=scope.is_unscoped,
            is_manager=scope.is_manager,
            branch_ids=scope.branch_ids,
            principal_kind=scope.principal.kind,
            principal_id=scope.principal.principal_id,
        )
        _validate_filters(request)
        qs = apply_filters(
            request,
            qs,
            filter_fields=("status", "branch"),
            ordering_fields=("starts_at",),
            default_ordering="starts_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([_present(request, m) for m in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        _validate_query(request, allowed=set())
        return _create(request, body=read_json(request))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/meetings/{pk}/",
    operations=MEETING_DETAIL_CONTRACTS,
)
@csrf_exempt
@require_auth
def meeting_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    _validate_query(request, allowed=set())
    return success(_present(request, _get_visible(request, pk)))


@openapi_contract(
    path="/api/v1/meetings/{pk}/cancel/",
    operations=(MEETING_CANCEL_CONTRACT,),
)
@csrf_exempt
@require_auth
def meeting_cancel_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _validate_query(request, allowed=set())
    meeting = _get_visible(request, pk)
    scope = _scope(request)
    _assert_branch_in_scope(scope.is_unscoped, meeting.branch_id, scope.branch_ids)
    _body(read_json(request), allowed=frozenset())
    return success(
        _present(
            request,
            _service().cancel(
                meeting,
                actor=request.user,
                actor_principal_kind=scope.principal.kind,
                actor_principal_id=scope.principal.principal_id,
            ),
        )
    )


@openapi_contract(
    path="/api/v1/meetings/{pk}/respond/",
    operations=(MEETING_RESPOND_CONTRACT,),
)
@csrf_exempt
@require_auth
def meeting_respond_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    deny_read_only_token(request)
    _validate_query(request, allowed=set())
    meeting = _get_visible(request, pk)  # invitees RSVP without a write perm; row-scoped
    body = _body(read_json(request), allowed=frozenset({"response"}))
    response = str_field(body, "response")
    if response not in (MeetingAttendee.Response.ACCEPTED, MeetingAttendee.Response.DECLINED):
        raise ValidationException(
            "Invalid response.",
            code="validation_error",
            fields={"response": ["Must be accepted or declined."]},
        )
    scope = _scope(request)
    _service().respond(
        meeting,
        user=request.user,
        principal_kind=scope.principal.kind,
        principal_id=scope.principal.principal_id,
        response=response,
    )
    return success(_present(request, _get_visible(request, pk)))


@openapi_contract(
    path="/api/v1/meetings/upcoming/",
    operations=MEETING_UPCOMING_CONTRACTS,
)
@csrf_exempt
@require_auth
def meetings_upcoming_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    _validate_query(request, allowed={"page", "page_size"})
    scope = _scope(request)
    items, total, page, size = paginate(
        request,
        _service().upcoming_for(
            principal_kind=scope.principal.kind,
            principal_id=scope.principal.principal_id,
        ),
    )
    return paginated([_present(request, m) for m in items], total=total, page=page, page_size=size)


# --- helpers ---------------------------------------------------------------
def _create(request: HttpRequest, *, body: dict[str, Any]) -> HttpResponse:
    body = _body(body, allowed=_CREATE_FIELDS)
    title = str_field(body, "title", max_length=200).strip()
    if not title:
        raise ValidationException(
            "Title is required.", code="validation_error", fields={"title": ["This field is required."]}
        )
    # Parse + validate the body (400s) BEFORE the branch-scope check (403) — matches
    # the old serializer-before-perform_create ordering.
    dto = ScheduleMeetingDTO(
        title=title,
        agenda=str_field(body, "agenda", max_length=MAX_MEETING_AGENDA_CHARS).strip(),
        location=str_field(body, "location", max_length=200).strip(),
        starts_at=_datetime(body, "starts_at"),
        ends_at=_datetime(body, "ends_at"),
        branch_id=int_field(body, "branch"),
        attendee_ids=_int_list(body, "attendees"),
        invitee_principals=_principal_targets(body, "invitees"),
    )
    scope = _scope(request)
    _assert_branch_in_scope(
        scope.is_unscoped, dto.branch_id, scope.branch_ids
    )  # fail closed before querying a branch outside the caller's authority
    service = _service()
    branch = service.resolve_branch(dto.branch_id)  # 400 only for an in-scope archived/missing row
    attendees = service.resolve_attendees(
        dto.attendee_ids,
        principal_targets=dto.invitee_principals,
        branch_id=dto.branch_id,
    )
    return created(
        _present(
            request,
            service.schedule(
                dto,
                created_by=request.user,
                created_by_principal_kind=scope.principal.kind,
                created_by_principal_id=scope.principal.principal_id,
                branch=branch,
                attendees=attendees,
            ),
        )
    )


def _validate_filters(request: HttpRequest) -> None:
    allowed = {"status", "branch", "ordering", "page", "page_size"}
    _validate_query(request, allowed=allowed)
    status = request.GET.get("status")
    if "status" in request.GET and status not in StaffMeeting.Status.values:
        raise ValidationException(
            "Invalid status filter.",
            code="validation_error",
            fields={"status": [f"Must be one of: {', '.join(StaffMeeting.Status.values)}."]},
        )
    if "branch" in request.GET and positive_int_filter(request, "branch") is None:
        raise ValidationException(
            "Invalid branch filter.",
            code="validation_error",
            fields={"branch": ["Must be a positive integer."]},
        )
    ordering = request.GET.get("ordering")
    if "ordering" in request.GET and ordering not in {"starts_at", "-starts_at"}:
        raise ValidationException(
            "Invalid ordering.",
            code="validation_error",
            fields={"ordering": ["Choose starts_at or -starts_at."]},
        )


def _validate_query(request: HttpRequest, *, allowed: set[str]) -> None:
    unknown = sorted(set(request.GET) - allowed)
    if unknown:
        raise ValidationException(
            "Unknown query parameter.",
            code="validation_error",
            fields={field: ["Unknown query parameter."] for field in unknown},
        )
    duplicates = sorted(name for name in request.GET if len(request.GET.getlist(name)) != 1)
    if duplicates:
        raise ValidationException(
            "Query parameter may be supplied only once.",
            code="validation_error",
            fields={field: ["Supply this parameter once."] for field in duplicates},
        )
    validate_pagination_filters(request)


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
    if len(raw) > MAX_MEETING_ATTENDEES:
        raise ValidationException(
            "Too many attendees.",
            code="validation_error",
            fields={name: [f"At most {MAX_MEETING_ATTENDEES} attendees are allowed."]},
        )
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ValidationException(
                "Invalid id.", code="validation_error", fields={name: ["Each item must be an id."]}
            )
        try:
            value = int(item)
        except (TypeError, ValueError):
            raise ValidationException(
                "Invalid id.", code="validation_error", fields={name: ["Each item must be an integer id."]}
            ) from None
        if value <= 0:
            raise ValidationException(
                "Invalid id.",
                code="validation_error",
                fields={name: ["Each item must be a positive integer id."]},
            )
        out.append(value)
    if len(out) != len(set(out)):
        raise ValidationException(
            "Duplicate attendee.",
            code="validation_error",
            fields={name: ["Each attendee may appear only once."]},
        )
    return out


def _principal_targets(body: dict[str, Any], name: str) -> list[MeetingPrincipalTarget]:
    raw = body.get(name, [])
    if not isinstance(raw, list):
        raise ValidationException(
            "Invalid invitee list.",
            code="validation_error",
            fields={name: ["Must be a list of role-account objects."]},
        )
    if len(raw) > MAX_MEETING_ATTENDEES:
        raise ValidationException(
            "Too many invitees.",
            code="validation_error",
            fields={name: [f"At most {MAX_MEETING_ATTENDEES} invitees are allowed."]},
        )
    targets: list[MeetingPrincipalTarget] = []
    seen: set[tuple[str, int]] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"kind", "id"}:
            raise ValidationException(
                "Invalid invitee.",
                code="validation_error",
                fields={name: ["Each invitee must contain only kind and id."]},
            )
        kind = item.get("kind")
        principal_id = item.get("id")
        if (
            kind not in STAFF_PRINCIPAL_KINDS
            or isinstance(principal_id, bool)
            or not isinstance(principal_id, int)
            or principal_id <= 0
        ):
            raise ValidationException(
                "Invalid invitee.",
                code="validation_error",
                fields={name: ["Choose a valid staff or teacher role account."]},
            )
        key = (str(kind), principal_id)
        if key in seen:
            raise ValidationException(
                "Duplicate invitee.",
                code="validation_error",
                fields={name: ["Each role account may appear only once."]},
            )
        seen.add(key)
        targets.append(MeetingPrincipalTarget(principal_kind=str(kind), principal_id=principal_id))
    return targets


def _body(body: dict[str, Any], *, allowed: frozenset[str]) -> dict[str, Any]:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationException(
            "Request contains unknown fields.",
            code="validation_error",
            fields={field: ["Unknown field."] for field in unknown},
        )
    return body
