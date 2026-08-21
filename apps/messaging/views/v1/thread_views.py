"""In-app messaging endpoints — plain Django views over the layered stack.

Strict participant isolation: you only ever resolve threads you're a member of, so every
detail/action is participant-gated (an out-of-scope thread 404s). Opening a thread is
messaging:write; reading + listing is messaging:read; POSTing a message additionally
requires messaging:write (so an A-2 write-revoke makes a role read-only). Message edits,
soft deletion, and reaction changes retain append-only evidence and durable realtime
pointers; only the exact sending principal may edit or delete a message.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.messaging.dto.thread_dto import CreateThreadDTO
from apps.messaging.interfaces.services import IThreadService
from apps.messaging.openapi_contracts import (
    MESSAGE_DELETE_CONTRACT,
    MESSAGE_PATCH_CONTRACT,
    MESSAGE_REACTION_DELETE_CONTRACT,
    MESSAGE_REACTION_POST_CONTRACT,
    THREAD_EVENTS_GET_CONTRACT,
    THREAD_EVENTS_HEAD_CONTRACT,
    THREAD_READ_POST_CONTRACT,
)
from apps.messaging.presenters import (
    contact_to_dict,
    message_to_dict,
    thread_event_page_to_dict,
    thread_read_state_to_dict,
    thread_to_dict,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.openapi_contracts import openapi_contract
from core.responses import created, error, no_content, paginated, success
from core.role_principals import RolePrincipal, request_role_principal

_RESOURCE = "messaging"
_MAX_PARTICIPANTS = 100
_MAX_ATTACHMENTS = 10
_MAX_MESSAGE_WINDOW = timedelta(hours=26)
_EVENT_PAGE_DEFAULT = 50
_EVENT_PAGE_MAX = 100
_MAX_DATABASE_ID = 9_223_372_036_854_775_807


def _service() -> IThreadService:
    return container.resolve(IThreadService)  # type: ignore[type-abstract]


def _viewer_id(request: HttpRequest) -> int:
    user: Any = request.user  # a real User post-@require_auth (typed User|AnonymousUser)
    return user.pk


def _viewer_principal(request: HttpRequest) -> RolePrincipal:
    cached = getattr(request, "_messaging_role_principal", None)
    if isinstance(cached, RolePrincipal):
        return cached
    principal = request_role_principal(
        request,
        error_code="messaging_principal_unavailable",
    )
    request._messaging_role_principal = principal  # type: ignore[attr-defined]
    return principal


def _unread_map(request: HttpRequest, threads: list) -> dict[int, int]:
    """One bounded query for {thread_id: unread_count} across the given threads."""
    principal = _viewer_principal(request)
    return _service().unread_counts(
        thread_ids=[t.id for t in threads],
        viewer_id=_viewer_id(request),
        viewer_principal_kind=principal.kind,
        viewer_principal_id=principal.principal_id,
    )


def _get_thread(request: HttpRequest, pk: int):
    principal = _viewer_principal(request)
    thread = _service().get_thread(
        user=request.user,
        principal_kind=principal.kind,
        principal_id=principal.principal_id,
        pk=pk,
    )
    if thread is None:
        raise NotFoundException(code="not_found")  # non-participant -> 404, strict isolation
    return thread


def _get_message(request: HttpRequest, pk: int):
    principal = _viewer_principal(request)
    message = _service().get_message(
        user=request.user,
        principal_kind=principal.kind,
        principal_id=principal.principal_id,
        pk=pk,
    )
    if message is None:
        raise NotFoundException(code="not_found")
    return message


def _message_window(request: HttpRequest):
    """Return a bounded, timezone-aware half-open message range.

    Mobile calendar days are converted to UTC by the client. Requiring both
    bounds prevents a date jump from accidentally turning into an unbounded
    history scan, while the half-open interval keeps adjacent days disjoint.
    """
    raw_gte = request.GET.get("created_at_gte", "").strip()
    raw_lt = request.GET.get("created_at_lt", "").strip()
    if not raw_gte and not raw_lt:
        return None
    if not raw_gte or not raw_lt:
        raise ValidationException(
            "created_at_gte and created_at_lt must be provided together.",
            code="validation_error",
            fields={
                "created_at_gte": ["Provide both UTC bounds."],
                "created_at_lt": ["Provide both UTC bounds."],
            },
        )

    try:
        lower = parse_datetime(raw_gte)
        upper = parse_datetime(raw_lt)
    except (OverflowError, ValueError):
        lower = upper = None
    if lower is None or upper is None or not timezone.is_aware(lower) or not timezone.is_aware(upper):
        raise ValidationException(
            "Message date bounds must be timezone-aware ISO-8601 datetimes.",
            code="validation_error",
            fields={
                "created_at_gte": ["Use an ISO-8601 datetime with an offset or Z."],
                "created_at_lt": ["Use an ISO-8601 datetime with an offset or Z."],
            },
        )
    if lower >= upper:
        raise ValidationException(
            "created_at_gte must be earlier than created_at_lt.",
            code="validation_error",
            fields={"created_at_lt": ["Must be later than created_at_gte."]},
        )
    if upper - lower > _MAX_MESSAGE_WINDOW:
        raise ValidationException(
            "A message date window cannot exceed 26 hours.",
            code="validation_error",
            fields={"created_at_lt": ["Choose one local calendar day."]},
        )
    return lower, upper


def _query_int(
    request: HttpRequest,
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    values = request.GET.getlist(name)
    if not values:
        return default
    raw = values[0]
    if len(values) != 1 or not raw or raw != raw.strip() or not raw.isascii() or not raw.isdecimal():
        raise ValidationException(
            f"{name} must be an integer.",
            code="validation_error",
            fields={name: ["Provide one decimal integer."]},
        )
    # Avoid both Python's deliberately bounded huge-integer conversion and a
    # PostgreSQL bigint overflow reaching the ORM as a 500 response.
    if len(raw) > 19:
        raise ValidationException(
            f"{name} is outside the supported range.",
            code="validation_error",
            fields={name: ["The value is too large."]},
        )
    value = int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise ValidationException(
            f"{name} is outside the supported range.",
            code="validation_error",
            fields={name: [f"Must be at least {minimum}{upper}."]},
        )
    return value


@csrf_exempt
@require_auth
def contacts_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    category = request.GET.get("category", "").strip().lower()
    if category not in ("", "staff", "student", "parent"):
        raise ValidationException(
            "category must be staff, student, or parent.",
            code="validation_error",
            fields={"category": ["Choose staff, student, or parent."]},
        )
    # Keep the role-native principal on the request attached all the way through
    # directory scoping. A bridge User may back more than one account kind.
    qs = _service().contacts(authorization_context=request, category=category)
    qs = apply_filters(
        request,
        qs,
        search_fields=(
            "username",
            "staff_profile__first_name",
            "staff_profile__middle_name",
            "staff_profile__last_name",
            "teacher_profile__first_name",
            "teacher_profile__middle_name",
            "teacher_profile__last_name",
            "student_profile__first_name",
            "student_profile__middle_name",
            "student_profile__last_name",
            "parent_profile__first_name",
            "parent_profile__middle_name",
            "parent_profile__last_name",
        ),
    )
    items, total, page, size = paginate(request, qs)
    return paginated(
        [contact_to_dict(contact) for contact in items],
        total=total,
        page=page,
        page_size=size,
        pagination_extra={"self_user_id": _viewer_id(request)},
    )


@csrf_exempt
@require_auth
def threads_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:read")
        principal = _viewer_principal(request)
        qs = _service().scoped_threads(
            user=request.user,
            principal_kind=principal.kind,
            principal_id=principal.principal_id,
        )
        # Meta.ordering is compound ("-last_message_at","-created_at") -> omit
        # default_ordering so apply_filters preserves it (only ?ordering re-orders).
        qs = apply_filters(request, qs, ordering_fields=("last_message_at", "created_at"))
        items, total, page, size = paginate(request, qs)
        unread = _unread_map(request, items)
        rows = [
            thread_to_dict(
                t,
                unread_count=unread.get(t.id, 0),
                viewer_id=_viewer_id(request),
                viewer_principal_kind=principal.kind,
                viewer_principal_id=principal.principal_id,
            )
            for t in items
        ]
        return paginated(rows, total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create_thread(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def thread_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD", "DELETE"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    thread = _get_thread(request, pk)
    principal = _viewer_principal(request)
    if request.method == "DELETE":
        _service().hide_thread(
            thread=thread,
            user=request.user,
            principal_kind=principal.kind,
            principal_id=principal.principal_id,
        )
        return no_content()
    unread = _unread_map(request, [thread])
    return success(
        thread_to_dict(
            thread,
            unread_count=unread.get(thread.id, 0),
            viewer_id=_viewer_id(request),
            viewer_principal_kind=principal.kind,
            viewer_principal_id=principal.principal_id,
        )
    )


@csrf_exempt
@require_auth
def thread_messages_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD", "POST"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")  # floor for both list + send
    thread = _get_thread(request, pk)  # participant gate (404) BEFORE the write check
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")  # posting additionally needs write
        return _send_message(request, thread)
    qs = _service().messages_of(thread=thread)
    after_id = _query_int(
        request,
        "after_id",
        default=0,
        minimum=0,
        maximum=_MAX_DATABASE_ID,
    )
    if after_id:
        qs = qs.filter(pk__gt=after_id)
    window = _message_window(request)
    if window is not None:
        lower, upper = window
        qs = qs.filter(created_at__gte=lower, created_at__lt=upper)
    items, total, page, size = paginate(request, qs)
    principal = _viewer_principal(request)
    return paginated(
        [
            message_to_dict(
                message,
                viewer_principal_kind=principal.kind,
                viewer_principal_id=principal.principal_id,
            )
            for message in items
        ],
        total=total,
        page=page,
        page_size=size,
    )


@csrf_exempt
@require_auth
@openapi_contract(
    path="/api/v1/messaging/messages/{pk}/",
    operations=(MESSAGE_PATCH_CONTRACT, MESSAGE_DELETE_CONTRACT),
)
def message_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("PATCH", "DELETE"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    message = _get_message(request, pk)
    check_perm(request, f"{_RESOURCE}:write")
    principal = _viewer_principal(request)
    if request.method == "DELETE":
        _service().delete_message(
            message=message,
            actor=request.user,
            actor_principal_kind=principal.kind,
            actor_principal_id=principal.principal_id,
        )
        return no_content()

    body = read_json(request)
    unknown = sorted(set(body) - {"body", "expected_version"})
    if unknown:
        raise ValidationException(
            "Unknown message edit field.",
            code="validation_error",
            fields={name: ["This field is not supported."] for name in unknown},
        )
    if "body" not in body:
        raise ValidationException(
            "body is required.",
            code="validation_error",
            fields={"body": ["This field is required."]},
        )
    text = str_field(body, "body", max_length=10_000)
    expected_version = int_field(
        body,
        "expected_version",
        required=False,
        min_value=1,
        max_value=_MAX_DATABASE_ID,
    )
    updated = _service().edit_message(
        message=message,
        actor=request.user,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        body=text,
        expected_version=expected_version,
    )
    return success(
        message_to_dict(
            updated,
            viewer_principal_kind=principal.kind,
            viewer_principal_id=principal.principal_id,
        )
    )


@csrf_exempt
@require_auth
@openapi_contract(
    path="/api/v1/messaging/messages/{pk}/reactions/",
    operations=(MESSAGE_REACTION_POST_CONTRACT,),
)
def message_reactions_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    message = _get_message(request, pk)
    check_perm(request, f"{_RESOURCE}:write")
    body = read_json(request)
    unknown = sorted(set(body) - {"emoji"})
    if unknown:
        raise ValidationException(
            "Unknown reaction field.",
            code="validation_error",
            fields={name: ["This field is not supported."] for name in unknown},
        )
    if "emoji" not in body:
        raise ValidationException(
            "emoji is required.",
            code="validation_error",
            fields={"emoji": ["This field is required."]},
        )
    emoji = str_field(body, "emoji", max_length=16)
    principal = _viewer_principal(request)
    updated = _service().add_reaction(
        message=message,
        actor=request.user,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        emoji=emoji,
    )
    return success(
        message_to_dict(
            updated,
            viewer_principal_kind=principal.kind,
            viewer_principal_id=principal.principal_id,
        )
    )


@csrf_exempt
@require_auth
@openapi_contract(
    path="/api/v1/messaging/messages/{pk}/reactions/{emoji}/",
    operations=(MESSAGE_REACTION_DELETE_CONTRACT,),
)
def message_reaction_detail_view(request: HttpRequest, pk: int, emoji: str) -> HttpResponse:
    if request.method != "DELETE":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    message = _get_message(request, pk)
    check_perm(request, f"{_RESOURCE}:write")
    principal = _viewer_principal(request)
    _service().remove_reaction(
        message=message,
        actor=request.user,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        emoji=emoji,
    )
    return no_content()


@csrf_exempt
@require_auth
@openapi_contract(
    path="/api/v1/messaging/threads/{pk}/events/",
    operations=(THREAD_EVENTS_GET_CONTRACT, THREAD_EVENTS_HEAD_CONTRACT),
)
def thread_events_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Durable cursor recovery for the pointer-only thread WebSocket stream."""

    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    unknown = sorted(set(request.GET) - {"after", "limit"})
    if unknown:
        raise ValidationException(
            "Unknown event query parameter.",
            code="validation_error",
            fields={name: ["This query parameter is not supported."] for name in unknown},
        )
    thread = _get_thread(request, pk)
    after = _query_int(
        request,
        "after",
        default=0,
        minimum=0,
        maximum=_MAX_DATABASE_ID,
    )
    limit = _query_int(
        request,
        "limit",
        default=_EVENT_PAGE_DEFAULT,
        minimum=1,
        maximum=_EVENT_PAGE_MAX,
    )
    page = _service().event_page(thread=thread, after=after, limit=limit)
    payload = thread_event_page_to_dict(page, thread_id=thread.pk)
    payload["generated_at"] = timezone.now().isoformat()
    return success(payload)


@csrf_exempt
@require_auth
@openapi_contract(
    path="/api/v1/messaging/threads/{pk}/read/",
    operations=(THREAD_READ_POST_CONTRACT,),
)
def thread_read_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    thread = _get_thread(request, pk)
    principal = _viewer_principal(request)
    body = read_json(request)
    unknown = sorted(set(body) - {"through_message_id"})
    if unknown:
        raise ValidationException(
            "Unknown read-state field.",
            code="validation_error",
            fields={name: ["This field is not supported."] for name in unknown},
        )
    through_message_id = int_field(
        body,
        "through_message_id",
        required=False,
        min_value=1,
        max_value=_MAX_DATABASE_ID,
    )
    state = _service().mark_read(
        thread=thread,
        user=request.user,
        principal_kind=principal.kind,
        principal_id=principal.principal_id,
        through_message_id=through_message_id,
    )
    return success(thread_read_state_to_dict(state, thread_id=thread.pk))


@csrf_exempt
@require_auth
def thread_preferences_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "PATCH":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    thread = _get_thread(request, pk)
    body = read_json(request)
    unknown = sorted(set(body) - {"notifications_muted", "archived"})
    if unknown:
        raise ValidationException(
            "Unknown conversation preference.",
            code="validation_error",
            fields={name: ["This field is not supported."] for name in unknown},
        )
    if not body:
        raise ValidationException(
            "Choose at least one conversation preference.",
            code="validation_error",
            fields={"preferences": ["Provide a preference to update."]},
        )
    if "notifications_muted" in body and not isinstance(body["notifications_muted"], bool):
        raise ValidationException(
            "notifications_muted must be a boolean.",
            code="validation_error",
            fields={"notifications_muted": ["Provide true or false."]},
        )
    if "archived" in body and not isinstance(body["archived"], bool):
        raise ValidationException(
            "archived must be a boolean.",
            code="validation_error",
            fields={"archived": ["Provide true or false."]},
        )
    principal = _viewer_principal(request)
    if "notifications_muted" in body:
        _service().set_notifications_muted(
            thread=thread,
            user=request.user,
            principal_kind=principal.kind,
            principal_id=principal.principal_id,
            muted=body["notifications_muted"],
        )
    if "archived" in body:
        _service().set_archived(
            thread=thread,
            user=request.user,
            principal_kind=principal.kind,
            principal_id=principal.principal_id,
            archived=body["archived"],
        )
    return success({key: body[key] for key in ("notifications_muted", "archived") if key in body})


@csrf_exempt
@require_auth
def attachment_upload_url_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    body = read_json(request)
    filename = str_field(body, "filename", max_length=255)
    if not filename.strip():
        raise ValidationException(
            "filename is required.", code="validation_error", fields={"filename": ["Required."]}
        )
    size_bytes = int_field(body, "size_bytes", required=True)
    if size_bytes is None or size_bytes < 1:
        raise ValidationException(
            "size_bytes must be positive.",
            code="validation_error",
            fields={"size_bytes": ["Must be at least 1."]},
        )
    content_type = str_field(body, "content_type", default="application/octet-stream", max_length=127).strip()
    return success(
        _service().presign_attachment(
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            requested_by=request.user,
        )
    )


@csrf_exempt
@require_auth
def thread_attachment_download_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    thread = _get_thread(request, pk)
    key = request.GET.get("key", "")
    if not key or key != key.strip() or len(key) > 512 or "\x00" in key:
        raise ValidationException(
            "key is required.", code="validation_error", fields={"key": ["Provide a valid attachment key."]}
        )
    return success({"url": _service().attachment_download_url(thread=thread, key=key), "expires_in": 300})


# --- helpers ---------------------------------------------------------------
def _create_thread(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    dto = CreateThreadDTO(
        participant_ids=_participant_ids(body),
        subject=str_field(body, "subject", max_length=200).strip(),
        first_body=str_field(body, "first_body").strip(),
        attachments=_attachments(body),
    )
    thread = _service().create(dto, authorization_context=request)
    principal = _viewer_principal(request)
    # A freshly created thread's only message is the creator's own opener -> unread 0.
    return created(
        thread_to_dict(
            thread,
            unread_count=0,
            viewer_id=_viewer_id(request),
            viewer_principal_kind=principal.kind,
            viewer_principal_id=principal.principal_id,
        )
    )


def _send_message(request: HttpRequest, thread) -> HttpResponse:
    body = read_json(request)
    text = str_field(body, "body").strip()
    attachments = _attachments(body)
    if not text and not attachments:
        raise ValidationException(
            "A message needs text or an attachment.",
            code="validation_error",
            fields={"body": ["Provide text or at least one attachment."]},
        )
    principal = _viewer_principal(request)
    message = _service().post(
        thread=thread,
        sender=request.user,
        sender_principal_kind=principal.kind,
        sender_principal_id=principal.principal_id,
        body=text,
        attachments=attachments,
    )
    return created(
        message_to_dict(
            message,
            viewer_principal_kind=principal.kind,
            viewer_principal_id=principal.principal_id,
        )
    )


def _participant_ids(body: dict[str, Any]) -> list[int]:
    """Bridge User ids from ``/messaging/contacts/`` (deduped, order-preserving).

    These are deliberately not role-profile ids returned as ``id`` by
    ``/users/me/``. Each value must be an integer; the legacy DRF ListField enforced
    the same input rule.
    """
    raw = body.get("participant_ids")
    if not isinstance(raw, list) or not raw:
        raise ValidationException(
            "participant_ids must be a non-empty list.",
            code="validation_error",
            fields={"participant_ids": ["Provide at least one participant id."]},
        )
    if len(raw) > _MAX_PARTICIPANTS:
        raise ValidationException(
            "Too many participants.",
            code="validation_error",
            fields={"participant_ids": [f"At most {_MAX_PARTICIPANTS} participants are allowed."]},
        )
    ids: list[int] = []
    for value in raw:
        if isinstance(value, bool):
            raise _bad_ids()
        if isinstance(value, int):
            ids.append(value)
        elif isinstance(value, str):
            try:
                ids.append(int(value))
            except ValueError:
                raise _bad_ids() from None
        else:
            raise _bad_ids()
    return list(dict.fromkeys(ids))


def _bad_ids() -> ValidationException:
    return ValidationException(
        "Each participant id must be an integer.",
        code="validation_error",
        fields={"participant_ids": ["Each id must be an integer."]},
    )


def _attachments(body: dict[str, Any]) -> list[str]:
    """Validated, unique messaging upload-grant keys; explicit null is invalid."""
    if "attachments" not in body:
        return []
    raw = body["attachments"]
    if not isinstance(raw, list):
        raise ValidationException(
            "attachments must be a list.",
            code="validation_error",
            fields={"attachments": ["Must be a list of keys."]},
        )
    if len(raw) > _MAX_ATTACHMENTS:
        raise ValidationException(
            "Too many attachments.",
            code="validation_error",
            fields={"attachments": [f"At most {_MAX_ATTACHMENTS} attachments are allowed."]},
        )
    if any(not isinstance(key, str) or not key or key != key.strip() or len(key) > 512 for key in raw):
        raise ValidationException(
            "Invalid attachment key.",
            code="validation_error",
            fields={"attachments": ["Each key must be non-empty text of at most 512 characters."]},
        )
    keys = list(raw)
    if len(keys) != len(set(keys)):
        raise ValidationException(
            "Duplicate attachment key.",
            code="validation_error",
            fields={"attachments": ["Attachment keys must be unique."]},
        )
    return keys
