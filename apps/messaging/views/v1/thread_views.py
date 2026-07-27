"""In-app messaging endpoints — plain Django views over the layered stack.

Strict participant isolation: you only ever resolve threads you're a member of, so every
detail/action is participant-gated (an out-of-scope thread 404s). Opening a thread is
messaging:write; reading + listing is messaging:read; POSTing a message additionally
requires messaging:write (so an A-2 write-revoke makes a role read-only). Messages are
append-only. No PUT/PATCH/DELETE (405).
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.messaging.dto.thread_dto import CreateThreadDTO
from apps.messaging.interfaces.services import IThreadService
from apps.messaging.presenters import message_to_dict, thread_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import read_json, str_field
from core.listing import apply_filters, paginate
from core.responses import created, error, paginated, success

_RESOURCE = "messaging"


def _service() -> IThreadService:
    return container.resolve(IThreadService)  # type: ignore[type-abstract]


def _viewer_id(request: HttpRequest) -> int:
    user: Any = request.user  # a real User post-@require_auth (typed User|AnonymousUser)
    return user.pk


def _unread_map(request: HttpRequest, threads: list) -> dict[int, int]:
    """One bounded query for {thread_id: unread_count} across the given threads."""
    return _service().unread_counts(
        thread_ids=[t.id for t in threads], viewer_id=_viewer_id(request)
    )


def _get_thread(request: HttpRequest, pk: int):
    thread = _service().get_thread(user=request.user, pk=pk)
    if thread is None:
        raise NotFoundException(code="not_found")  # non-participant -> 404, strict isolation
    return thread


@csrf_exempt
@require_auth
def threads_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:read")
        qs = _service().scoped_threads(user=request.user)
        # Meta.ordering is compound ("-last_message_at","-created_at") -> omit
        # default_ordering so apply_filters preserves it (only ?ordering re-orders).
        qs = apply_filters(request, qs, ordering_fields=("last_message_at", "created_at"))
        items, total, page, size = paginate(request, qs)
        unread = _unread_map(request, items)
        rows = [thread_to_dict(t, unread_count=unread.get(t.id, 0)) for t in items]
        return paginated(rows, total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create_thread(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def thread_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    thread = _get_thread(request, pk)
    unread = _unread_map(request, [thread])
    return success(thread_to_dict(thread, unread_count=unread.get(thread.id, 0)))


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
    items, total, page, size = paginate(request, qs)
    return paginated([message_to_dict(m) for m in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def thread_read_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    thread = _get_thread(request, pk)
    _service().mark_read(thread=thread, user=request.user)
    return success({"status": "ok"})


# --- helpers ---------------------------------------------------------------
def _create_thread(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    dto = CreateThreadDTO(
        participant_ids=_participant_ids(body),
        subject=str_field(body, "subject", max_length=200),
        first_body=str_field(body, "first_body"),
        attachments=_attachments(body),
    )
    thread = _service().create(dto, creator=request.user)
    # A freshly created thread's only message is the creator's own opener -> unread 0.
    return created(thread_to_dict(thread, unread_count=0))


def _send_message(request: HttpRequest, thread) -> HttpResponse:
    body = read_json(request)
    text = str_field(body, "body")
    if not text.strip():  # old SendMessageSerializer body was required + non-blank
        raise ValidationException(
            "A message body is required.",
            code="validation_error",
            fields={"body": ["This field may not be blank."]},
        )
    message = _service().post(thread=thread, sender=request.user, body=text, attachments=_attachments(body))
    return created(message_to_dict(message))


def _participant_ids(body: dict[str, Any]) -> list[int]:
    """A non-empty list of integer user ids (deduped, order-preserving). Each MUST be an
    int (a non-int would break `id__in` / the dict.fromkeys dedup, and an unhashable one
    would 500) — the old ListField(child=IntegerField()) enforced this."""
    raw = body.get("participant_ids")
    if not isinstance(raw, list) or not raw:
        raise ValidationException(
            "participant_ids must be a non-empty list.",
            code="validation_error",
            fields={"participant_ids": ["Provide at least one participant id."]},
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


def _attachments(body: dict[str, Any]) -> list:
    """The attachments field (S3 keys). Absent/null -> []; a non-list -> 400 (the column
    is a list of keys)."""
    raw = body.get("attachments")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationException(
            "attachments must be a list.",
            code="validation_error",
            fields={"attachments": ["Must be a list of keys."]},
        )
    return raw
