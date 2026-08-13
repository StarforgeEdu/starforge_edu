"""Messaging services (F4-4).

Preserved verbatim; the layered service (services/v1/thread_service.py) wraps
`create_thread` / `post_message` / `mark_read` after the view validates the body and
resolves participants. `post_message` fans out realtime notifications via
apps.notifications.dispatch (pointers only, never the body).
"""

from __future__ import annotations

import unicodedata
import uuid
from contextlib import suppress
from datetime import timedelta
from itertools import pairwise

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.messaging.dto.thread_dto import ThreadReadStateDTO
from apps.messaging.models import (
    DELIVERABLE_PARTICIPANT_STATUSES,
    Message,
    MessageAttachmentUploadGrant,
    MessageReaction,
    MessageRevision,
    MessageRevisionKind,
    ParticipantAttributionStatus,
    Thread,
    ThreadEventKind,
    ThreadParticipant,
    ThreadRealtimeEvent,
)
from apps.messaging.storage_keys import (
    final_attachment_key,
    parse_final_attachment_key,
    parse_legacy_attachment_key,
    parse_pending_attachment_key,
    pending_attachment_key,
)
from core.attachment_storage import (
    AttachmentObjectError,
    allowed_attachment_mime_types,
    promote_attachment_object,
)
from core.exceptions import (
    ConflictException,
    NotFoundException,
    PermissionException,
    UnprocessableEntity,
    ValidationException,
)
from core.storage_keys import normalized_storage_filename
from core.utils import current_schema
from infrastructure.storage.s3_client import delete_object, presign_download, presign_post_upload

_MAX_ATTACHMENTS = 10
_MAX_OTHER_PARTICIPANTS = 100
_MAX_ACTIVE_UPLOAD_GRANTS = 30
_UPLOAD_GRANT_SECONDS = 600
_MAX_MESSAGE_BODY_CHARS = 10_000
_MAX_REACTION_EMOJI_CHARS = 16
_MAX_REACTION_EMOJI_BYTES = 64
_MAX_DATABASE_ID = 9_223_372_036_854_775_807


@transaction.atomic
def presign_attachment_upload(*, filename: str, content_type: str, size_bytes: int, requested_by) -> dict:
    """Create an exact-size policy for an owner-bound staging object."""

    from apps.org.selectors import get_center_settings

    normalized_filename = normalized_storage_filename(filename)
    if normalized_filename is None:
        raise ValidationException(
            _("That filename is not allowed."),
            code="invalid_filename",
            fields={"filename": ["Provide one safe filename of at most 255 UTF-8 bytes."]},
        )
    filename = normalized_filename
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
        raise ValidationException(
            _("size_bytes must be positive."),
            code="validation_error",
            fields={"size_bytes": ["Must be at least 1."]},
        )
    content_type = content_type.partition(";")[0].strip().lower()
    if not content_type or len(content_type) > 127 or "/" not in content_type:
        raise ValidationException(
            _("A valid content type is required."),
            code="validation_error",
            fields={"content_type": ["Required; at most 127 characters."]},
        )

    settings_obj = get_center_settings()
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed = {
        str(ext).lower().lstrip(".") for ext in settings_obj.allowed_file_types if isinstance(ext, str)
    }
    if extension not in allowed:
        raise UnprocessableEntity(
            _("That file type is not allowed."),
            code="file_type_not_allowed",
            fields={"filename": [f"Extension '.{extension}' is not allowed."]},
        )
    expected_types = allowed_attachment_mime_types(filename)
    if not expected_types:
        raise UnprocessableEntity(
            _("That file type is not supported for attachments."),
            code="file_type_not_allowed",
            fields={"filename": [f"Extension '.{extension}' has no reviewed attachment signature."]},
        )
    if content_type not in expected_types:
        raise UnprocessableEntity(
            _("The content type does not match the filename."),
            code="content_type_mismatch",
            fields={"content_type": [f"'{content_type}' is not valid for '.{extension}'."]},
        )
    max_bytes = settings_obj.max_upload_mb * 1024 * 1024
    if size_bytes > max_bytes:
        raise UnprocessableEntity(
            _("That file is too large."),
            code="file_too_large",
            fields={"size_bytes": [f"Exceeds the {settings_obj.max_upload_mb} MB limit."]},
        )

    requested_by.__class__.objects.select_for_update().get(pk=requested_by.pk)
    now = timezone.now()
    active_grants = MessageAttachmentUploadGrant.objects.filter(
        requested_by=requested_by,
        consumed_at__isnull=True,
        expires_at__gt=now,
    ).count()
    if active_grants >= _MAX_ACTIVE_UPLOAD_GRANTS:
        raise UnprocessableEntity(
            _("Too many uploads are waiting to be attached."),
            code="upload_grant_limit",
            fields={"filename": ["Attach or let an earlier upload expire before requesting another."]},
        )

    key = pending_attachment_key(
        schema=current_schema(),
        owner_id=requested_by.pk,
        upload_id=uuid.uuid4().hex,
        filename=filename,
    )
    expires_at = now + timedelta(seconds=_UPLOAD_GRANT_SECONDS)
    grant = MessageAttachmentUploadGrant.objects.create(
        key=key,
        requested_by=requested_by,
        content_type=content_type,
        expected_size_bytes=size_bytes,
        expires_at=expires_at,
    )
    post = presign_post_upload(
        key,
        content_type=content_type,
        size_bytes=size_bytes,
        expires_in=_UPLOAD_GRANT_SECONDS,
    )
    return {
        "url": post["url"],
        "fields": post["fields"],
        "method": "POST",
        "key": key,
        "grant_id": grant.pk,
        "expires_at": expires_at.isoformat(),
    }


def _normalize_attachment_keys(keys: object) -> list[str]:
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise ValidationException(
            _("Attachments must be a list of upload keys."),
            code="validation_error",
            fields={"attachments": ["Each attachment key must be text."]},
        )
    if len(keys) > _MAX_ATTACHMENTS or len(keys) != len(set(keys)):
        raise ValidationException(
            _("Attachments must be unique and limited to ten files."),
            code="validation_error",
            fields={"attachments": ["Provide at most 10 unique keys."]},
        )
    if any(not key or len(key) > 512 for key in keys):
        raise ValidationException(
            _("One or more attachment keys are malformed."),
            code="validation_error",
            fields={"attachments": ["Each key must be non-empty text of at most 512 characters."]},
        )
    return list(keys)


def _object_error(reason: str) -> UnprocessableEntity:
    if reason == "missing":
        return UnprocessableEntity(
            _("The uploaded attachment could not be found."),
            code="attachment_not_uploaded",
            fields={"attachments": ["Upload the file before sending the message."]},
        )
    if reason == "size":
        return UnprocessableEntity(
            _("The uploaded attachment has the wrong size."),
            code="attachment_size_mismatch",
            fields={"attachments": ["The stored size does not match the upload grant."]},
        )
    if reason == "content_type":
        return UnprocessableEntity(
            _("The uploaded attachment has the wrong content type."),
            code="attachment_type_mismatch",
            fields={"attachments": ["The stored type does not match the upload grant."]},
        )
    return UnprocessableEntity(
        _("The uploaded attachment does not contain the declared file type."),
        code="attachment_content_mismatch",
        fields={"attachments": ["Upload a file whose contents match its filename and type."]},
    )


def _delete_promoted_objects(keys: list[str]) -> None:
    schema = current_schema()
    for key in keys:
        if parse_final_attachment_key(key, schema=schema) is None:
            continue
        with suppress(Exception):
            delete_object(key)


def _enqueue_source_cleanup(grant_ids: list[int]) -> None:
    if not grant_ids:
        return
    schema = current_schema()

    def enqueue() -> None:
        from celery_tasks.attachment_tasks import cleanup_consumed_upload_sources_for_schema

        cleanup_consumed_upload_sources_for_schema.delay("messaging", grant_ids, _schema_name=schema)

    transaction.on_commit(enqueue, robust=True)


def enqueue_attachment_deletions(keys: list[str]) -> None:
    if not keys:
        return
    schema = current_schema()
    unique_keys = list(dict.fromkeys(keys))
    key_chunks = [unique_keys[index : index + 500] for index in range(0, len(unique_keys), 500)]
    now = timezone.now()
    grant_ids: list[int] = []
    for chunk in key_chunks:
        grants = MessageAttachmentUploadGrant.objects.filter(
            consumed_at__isnull=False,
            durable_deleted_at__isnull=True,
        ).filter(Q(durable_key__in=chunk) | Q(durable_key__isnull=True, key__in=chunk))
        chunk_ids = list(grants.values_list("pk", flat=True))
        if chunk_ids:
            MessageAttachmentUploadGrant.objects.filter(pk__in=chunk_ids).update(deletion_requested_at=now)
            grant_ids.extend(chunk_ids)
    grant_ids = list(dict.fromkeys(grant_ids))
    grant_chunks = [grant_ids[index : index + 500] for index in range(0, len(grant_ids), 500)]

    def enqueue() -> None:
        from celery_tasks.attachment_tasks import delete_attachment_objects

        for chunk in grant_chunks:
            delete_attachment_objects.delay("messaging", chunk, _schema_name=schema)

    transaction.on_commit(enqueue, robust=True)


def _materialize_message_attachments(*, keys: object, actor, message: Message) -> list[str]:
    normalized = _normalize_attachment_keys(keys)
    if not normalized:
        return []
    schema = current_schema()
    parsed = {key: parse_pending_attachment_key(key, schema=schema) for key in normalized}
    if any(value is None or value.owner_id != actor.pk for value in parsed.values()):
        raise UnprocessableEntity(
            _("One or more attachment keys are not authorized."),
            code="invalid_attachment_key",
            fields={"attachments": ["Use keys returned by your messaging upload request."]},
        )

    now = timezone.now()
    grants = {
        grant.key: grant
        for grant in MessageAttachmentUploadGrant.objects.select_for_update().filter(
            key__in=normalized,
            requested_by=actor,
            consumed_at__isnull=True,
            expires_at__gt=now,
        )
    }
    if set(grants) != set(normalized):
        raise UnprocessableEntity(
            _("An attachment grant is missing, expired, used, or belongs to another user."),
            code="invalid_attachment_grant",
            fields={"attachments": ["Request a new upload URL and upload the file again."]},
        )

    promoted: list[str] = []
    try:
        for source_key in normalized:
            grant = grants[source_key]
            parsed_source = parsed[source_key]
            if parsed_source is None:  # guarded above
                raise UnprocessableEntity(code="invalid_attachment_key")
            destination_key = final_attachment_key(
                schema=schema,
                message_id=message.pk,
                grant_id=grant.pk,
                filename=parsed_source.filename,
            )
            try:
                verified = promote_attachment_object(
                    source_key=source_key,
                    destination_key=destination_key,
                    filename=parsed_source.filename,
                    expected_size_bytes=grant.expected_size_bytes,
                    expected_content_type=grant.content_type,
                )
            except AttachmentObjectError as exc:
                raise _object_error(exc.reason) from exc
            promoted.append(destination_key)
            grant.actual_size_bytes = verified.size_bytes
            grant.consumed_at = now
            grant.durable_key = destination_key
            grant.save(update_fields=["actual_size_bytes", "consumed_at", "durable_key"])
    except Exception:
        _delete_promoted_objects(promoted)
        raise
    _enqueue_source_cleanup([grants[key].pk for key in normalized])
    return promoted


def _trusted_final_attachment(message: Message, key: str) -> MessageAttachmentUploadGrant | None:
    schema = current_schema()
    parsed = parse_final_attachment_key(key, schema=schema)
    if parsed is None or parsed.message_id != message.pk:
        return None
    grant = MessageAttachmentUploadGrant.objects.filter(
        pk=parsed.grant_id,
        requested_by_id=message.sender_id,
        consumed_at__isnull=False,
        durable_deleted_at__isnull=True,
    ).first()
    if grant is None:
        return None
    source = parse_pending_attachment_key(grant.key, schema=schema)
    if source is None or source.filename != parsed.filename:
        return None
    expected_key = final_attachment_key(
        schema=schema,
        message_id=message.pk,
        grant_id=grant.pk,
        filename=source.filename,
    )
    return grant if key == expected_key and grant.durable_key in (None, key) else None


def _trusted_legacy_attachment(message: Message, key: str) -> MessageAttachmentUploadGrant | None:
    schema = current_schema()
    parsed = parse_legacy_attachment_key(key, schema=schema)
    if parsed is None or (message.sender_id is not None and parsed.owner_id != message.sender_id):
        return None
    grant = MessageAttachmentUploadGrant.objects.filter(
        key=key,
        requested_by_id=message.sender_id,
        consumed_at__isnull=False,
        durable_deleted_at__isnull=True,
    ).first()
    if grant is None:
        return None
    if grant.durable_key not in (None, key):
        return None
    message_ids = list(Message.objects.filter(attachments__contains=[key]).values_list("pk", flat=True)[:2])
    return grant if message_ids == [message.pk] else None


def trusted_message_attachment_keys(message: Message) -> tuple[str, ...]:
    raw = message.attachments if isinstance(message.attachments, list) else []
    return tuple(
        key
        for key in raw
        if isinstance(key, str)
        and (
            _trusted_final_attachment(message, key) is not None
            or _trusted_legacy_attachment(message, key) is not None
        )
    )


@transaction.atomic
def attachment_download_url(*, thread: Thread, key: str) -> str:
    """Sign only a server-issued object bound to an exact message in this thread."""

    if not key or len(key) > 512:
        raise NotFoundException(_("Attachment not found."), code="not_found")
    schema = current_schema()
    parsed = parse_final_attachment_key(key, schema=schema)
    if parsed is not None:
        message = (
            Message.objects.select_for_update()
            .filter(
                pk=parsed.message_id,
                thread=thread,
                attachments__contains=[key],
                deleted_at__isnull=True,
            )
            .first()
        )
        grant = _trusted_final_attachment(message, key) if message is not None else None
    elif parse_legacy_attachment_key(key, schema=schema) is not None:
        message = (
            Message.objects.select_for_update()
            .filter(
                thread=thread,
                attachments__contains=[key],
                deleted_at__isnull=True,
            )
            .first()
        )
        grant = _trusted_legacy_attachment(message, key) if message is not None else None
    else:
        grant = None
    if grant is None:
        raise NotFoundException(_("Attachment not found."), code="not_found")
    filename = key.rsplit("/", 1)[-1]
    return presign_download(
        key,
        expires_in=300,
        download_filename=filename,
        response_content_type=grant.content_type,
    )


def assert_thread_safeguarding(
    *,
    creator,
    participants: list,
    authorization_context=None,
    creator_principal=None,
    participant_principals: dict | None = None,
) -> None:
    """Reject unsafe participant sets using canonical account kinds.

    Callers establish the exact branch/department boundary before using this
    standalone check, so an out-of-scope guessed id never reveals its principal
    kind.
    """
    from apps.access.models import AccountType
    from core.role_principals import (
        RolePrincipal,
        request_role_principal,
        resolve_unambiguous_user_principal,
        resolve_unambiguous_user_principals,
    )

    members = list({creator.id: creator, **{u.id: u for u in participants}}.values())
    others = [u for u in members if u.id != creator.id]
    if authorization_context is not None and getattr(authorization_context, "user", None) != creator:
        raise PermissionException(
            _("The authenticated messaging principal is invalid."),
            code="forbidden",
        )
    if creator_principal is None:
        creator_principal = (
            request_role_principal(
                authorization_context,
                error_code="messaging_principal_unavailable",
            )
            if authorization_context is not None
            else resolve_unambiguous_user_principal(
                creator.id,
                field="creator",
                message=_("The creator does not identify one active role account."),
            )
        )
    if not isinstance(creator_principal, RolePrincipal) or creator_principal.user_id != creator.id:
        raise PermissionException(
            _("The authenticated messaging principal is invalid."),
            code="forbidden",
        )

    if participant_principals is None:
        participant_principals = resolve_unambiguous_user_principals(
            [user.id for user in others],
            field="participant_ids",
            message=_("One or more participants do not identify one active role account."),
        )
    if set(participant_principals) != {user.id for user in others} or any(
        principal.user_id != user_id for user_id, principal in participant_principals.items()
    ):
        raise ValidationException(
            _("One or more participant role accounts are invalid."),
            code="validation_error",
            fields={"participant_ids": [_("Choose active role accounts.")]},
        )

    principals = [creator_principal, *participant_principals.values()]

    def is_staff(principal) -> bool:
        return principal.kind in {
            AccountType.AccountKind.STAFF,
            AccountType.AccountKind.TEACHER,
        }

    # A non-staff opener can never create a student-to-student channel. A staff
    # or teacher opener remains a durable participant and may open a supervised
    # class conversation with students inside the exact recipient scope checked
    # below. This makes teacher-created cohort rooms safe and useful.
    if (
        not is_staff(creator_principal)
        and sum(1 for principal in principals if principal.kind == AccountType.AccountKind.STUDENT) > 1
    ):
        raise PermissionException(
            _("A conversation can include at most one student."), code="non_staff_recipient"
        )
    if not is_staff(creator_principal) and any(
        not is_staff(participant_principals[user.id]) for user in others
    ):
        raise PermissionException(_("You can only message staff."), code="non_staff_recipient")


@transaction.atomic
def create_thread(
    *,
    creator,
    participants: list,
    subject: str = "",
    first_body: str = "",
    attachments=None,
    authorization_context=None,
    creator_principal=None,
    participant_principals: dict | None = None,
) -> Thread:
    """Open a thread between the creator and `participants` (User objects).

    Safeguarding (dignity DNA): a non-staff opener (student/parent) may only message
    STAFF — never another student/parent — so the channel can't be used for
    unsupervised student-to-student contact. Staff may message anyone.
    """
    from core.permissions import get_unambiguous_user_authorization_context, get_user_roles
    from core.scoping import permission_membership_scopes

    members = list({creator.id: creator, **{u.id: u for u in participants}}.values())  # dedup, incl. creator
    others = [u for u in members if u.id != creator.id]
    if not others:
        raise ValidationException(
            _("A thread needs at least one other participant."), code="thread_needs_participant"
        )
    if len(others) > _MAX_OTHER_PARTICIPANTS:
        raise ValidationException(
            _("A thread has too many participants."),
            code="too_many_participants",
            fields={"participant_ids": ["At most 100 other participants are allowed."]},
        )
    # Resolve legacy bridge ids once, in a fixed number of role-table queries.
    # Both safeguarding and persistence consume the same exact evidence; resolving
    # again after authorization would add latency and create a needless TOCTOU gap.
    from core.role_principals import request_role_principal, resolve_unambiguous_user_principals

    missing_user_ids: list[int] = []
    if creator_principal is None:
        if authorization_context is not None:
            creator_principal = request_role_principal(
                authorization_context,
                error_code="messaging_principal_unavailable",
            )
        else:
            missing_user_ids.append(creator.id)
    if participant_principals is None:
        missing_user_ids.extend(user.id for user in others)
    resolved_principals = (
        resolve_unambiguous_user_principals(
            missing_user_ids,
            field="participant_ids",
            message=_("One or more participants do not identify one active role account."),
        )
        if missing_user_ids
        else {}
    )
    if creator_principal is None:
        creator_principal = resolved_principals.pop(creator.id)
    if participant_principals is None:
        participant_principals = resolved_principals

    assert_thread_safeguarding(
        creator=creator,
        participants=others,
        authorization_context=authorization_context,
        creator_principal=creator_principal,
        participant_principals=participant_principals,
    )
    if authorization_context is None:
        creator_roles, _memberships = get_unambiguous_user_authorization_context(creator)
    else:
        if getattr(authorization_context, "user", None) != creator:
            raise PermissionException(
                _("The authenticated messaging principal is invalid."),
                code="forbidden",
            )
        creator_roles = get_user_roles(authorization_context)
    creator_scopes = permission_membership_scopes(
        roles=creator_roles,
        permission="messaging:write",
    )
    if not creator.is_superuser and not creator_scopes:
        raise PermissionException(
            _("You do not have permission to open this conversation."),
            code="forbidden",
        )
    has_organization_scope = any(scope.is_organization_wide for scope in creator_scopes)
    creator_branch_ids = {scope.branch_id for scope in creator_scopes}
    creator_branch_id = (
        next(iter(creator_branch_ids))
        if not has_organization_scope and len(creator_branch_ids) == 1
        else None
    )

    thread = Thread.objects.create(subject=subject, created_by=creator, branch_id=creator_branch_id)
    principal_by_user = {creator.id: creator_principal, **participant_principals}
    ThreadParticipant.objects.bulk_create(
        [
            ThreadParticipant(
                thread=thread,
                user=user,
                principal_kind=principal_by_user[user.id].kind,
                principal_id=principal_by_user[user.id].principal_id,
                attribution_status=ParticipantAttributionStatus.CAPTURED,
            )
            for user in members
        ]
    )

    if first_body.strip() or attachments:
        post_message(
            thread=thread,
            sender=creator,
            sender_principal_kind=creator_principal.kind,
            sender_principal_id=creator_principal.principal_id,
            body=first_body,
            attachments=attachments,
        )
    return thread


def _publish_realtime_event(event: ThreadRealtimeEvent) -> None:
    """Publish one committed pointer event to the tenant-private thread group.

    Redis is an acceleration path only.  The durable event row is created in
    the same transaction as the domain change, and reconnecting clients recover
    it by cursor when this best-effort push cannot be delivered.
    """

    from infrastructure.websocket.channel_layer import group_send
    from infrastructure.websocket.groups import messaging_thread_group

    schema = current_schema()
    group = messaging_thread_group(schema, event.thread_id)
    payload = {
        "type": "messaging.thread.event",
        "thread_id": event.thread_id,
        "sequence": event.sequence,
        "event_kind": event.kind,
        "message_id": event.message_id,
        "actor_principal_kind": event.actor_principal_kind,
        "actor_principal_id": event.actor_principal_id,
        "created_at": event.created_at.isoformat(),
    }
    transaction.on_commit(lambda: group_send(group, payload), robust=True)


def _append_realtime_event(
    *,
    locked_thread: Thread,
    kind: str,
    actor,
    actor_principal_kind: str,
    actor_principal_id: int,
    message: Message,
) -> ThreadRealtimeEvent:
    """Allocate one gap-free cursor while the owning Thread row is locked."""

    sequence = int(locked_thread.realtime_sequence) + 1
    Thread.objects.filter(pk=locked_thread.pk).update(realtime_sequence=sequence)
    locked_thread.realtime_sequence = sequence
    event = ThreadRealtimeEvent.objects.create(
        thread=locked_thread,
        sequence=sequence,
        kind=kind,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
        message=message,
    )
    _publish_realtime_event(event)
    return event


def _normalize_reaction_emoji(value: str) -> str:
    emoji = unicodedata.normalize("NFC", value.strip())
    encoded = emoji.encode("utf-8")
    if not emoji or len(emoji) > _MAX_REACTION_EMOJI_CHARS or len(encoded) > _MAX_REACTION_EMOJI_BYTES:
        raise ValidationException(
            _("Choose one valid emoji reaction."),
            code="invalid_reaction",
            fields={"emoji": [_("Use one emoji of at most 16 Unicode characters.")]},
        )
    if any(
        char.isspace() or (unicodedata.category(char).startswith("C") and char not in {"\u200d", "\ufe0f"})
        for char in emoji
    ):
        raise ValidationException(
            _("Choose one valid emoji reaction."),
            code="invalid_reaction",
            fields={"emoji": [_("Text and control characters are not reactions.")]},
        )
    base_positions = [
        index
        for index, char in enumerate(emoji)
        if (unicodedata.category(char) == "So" or ord(char) >= 0x1F000)
        and not 0x1F3FB <= ord(char) <= 0x1F3FF
    ]
    has_keycap = "\u20e3" in emoji and emoji[0] in "#*0123456789"
    if not base_positions and not has_keycap:
        raise ValidationException(
            _("Choose one valid emoji reaction."),
            code="invalid_reaction",
            fields={"emoji": [_("Provide an emoji rather than text.")]},
        )
    regional = [index for index in base_positions if 0x1F1E6 <= ord(emoji[index]) <= 0x1F1FF]
    if regional and (len(regional) != 2 or len(base_positions) != 2):
        raise ValidationException(
            _("Choose one valid emoji reaction."),
            code="invalid_reaction",
            fields={"emoji": [_("Provide one complete flag emoji.")]},
        )
    if not regional and len(base_positions) > 1:
        for left, right in pairwise(base_positions):
            if "\u200d" not in emoji[left + 1 : right]:
                raise ValidationException(
                    _("Choose one valid emoji reaction."),
                    code="invalid_reaction",
                    fields={"emoji": [_("Provide only one emoji reaction.")]},
                )
    return emoji


def _lock_message_for_actor(
    *,
    message: Message,
    actor,
    actor_principal_kind: str,
    actor_principal_id: int,
) -> tuple[Thread, Message]:
    locked_thread = Thread.objects.select_for_update().filter(pk=message.thread_id).first()
    if locked_thread is None:
        raise NotFoundException(_("Message not found."), code="not_found")
    locked_message = Message.objects.select_for_update().filter(pk=message.pk, thread=locked_thread).first()
    if locked_message is None:
        raise NotFoundException(_("Message not found."), code="not_found")
    if not ThreadParticipant.objects.filter(
        thread=locked_thread,
        user=actor,
        principal_kind=actor_principal_kind,
        principal_id=actor_principal_id,
        attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
    ).exists():
        raise NotFoundException(_("Message not found."), code="not_found")
    return locked_thread, locked_message


def _assert_message_author(
    *,
    message: Message,
    actor,
    actor_principal_kind: str,
    actor_principal_id: int,
) -> None:
    if not (
        message.sender_id == actor.pk
        and message.sender_principal_kind == actor_principal_kind
        and message.sender_principal_id == actor_principal_id
        and message.sender_attribution_status in DELIVERABLE_PARTICIPANT_STATUSES
    ):
        raise PermissionException(
            _("Only the original sender can change this message."),
            code="message_not_owned",
        )


def _revision_before_mutation(
    *,
    message: Message,
    actor,
    actor_principal_kind: str,
    actor_principal_id: int,
    version: int,
    kind: str,
) -> MessageRevision:
    return MessageRevision.objects.create(
        message=message,
        version=version,
        kind=kind,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
        previous_body=message.body,
        previous_edited_at=message.edited_at,
        previous_deleted_at=message.deleted_at,
    )


def _refresh_active_reactions(message: Message) -> None:
    message.active_reactions = list(  # type: ignore[attr-defined]
        MessageReaction.objects.filter(message=message, removed_at__isnull=True).order_by("created_at", "id")
    )


@transaction.atomic
def edit_message(
    *,
    message: Message,
    actor,
    actor_principal_kind: str,
    actor_principal_id: int,
    body: str,
    expected_version: int | None = None,
) -> Message:
    body = body.strip()
    if not body:
        raise ValidationException(
            _("An edited message needs text."),
            code="empty_message",
            fields={"body": [_("Provide message text.")]},
        )
    if len(body) > _MAX_MESSAGE_BODY_CHARS:
        raise ValidationException(
            _("The message is too long."),
            code="validation_error",
            fields={"body": [_("Use at most 10,000 characters.")]},
        )
    locked_thread, locked_message = _lock_message_for_actor(
        message=message,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )
    _assert_message_author(
        message=locked_message,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )
    if expected_version is not None and expected_version != locked_message.version:
        raise ConflictException(
            _("The message changed before this edit was applied."),
            code="message_version_conflict",
        )
    if locked_message.deleted_at is not None:
        raise ConflictException(_("A deleted message cannot be edited."), code="message_deleted")
    if locked_message.body == body:
        _refresh_active_reactions(locked_message)
        return locked_message

    now = timezone.now()
    new_version = locked_message.version + 1
    _revision_before_mutation(
        message=locked_message,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
        version=new_version,
        kind=MessageRevisionKind.EDITED,
    )
    Message.objects.filter(pk=locked_message.pk).update(
        body=body,
        version=new_version,
        edited_at=now,
    )
    Thread.objects.filter(pk=locked_thread.pk).update(updated_at=now)
    locked_message.body = body
    locked_message.version = new_version
    locked_message.edited_at = now
    _append_realtime_event(
        locked_thread=locked_thread,
        kind=ThreadEventKind.MESSAGE_UPDATED,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
        message=locked_message,
    )
    _refresh_active_reactions(locked_message)
    return locked_message


@transaction.atomic
def delete_message(
    *,
    message: Message,
    actor,
    actor_principal_kind: str,
    actor_principal_id: int,
) -> Message:
    locked_thread, locked_message = _lock_message_for_actor(
        message=message,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )
    _assert_message_author(
        message=locked_message,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )
    if locked_message.deleted_at is not None:
        return locked_message

    now = timezone.now()
    new_version = locked_message.version + 1
    _revision_before_mutation(
        message=locked_message,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
        version=new_version,
        kind=MessageRevisionKind.DELETED,
    )
    Message.objects.filter(pk=locked_message.pk).update(
        version=new_version,
        deleted_at=now,
    )
    MessageReaction.objects.filter(message=locked_message, removed_at__isnull=True).update(removed_at=now)
    Thread.objects.filter(pk=locked_thread.pk).update(updated_at=now)
    locked_message.version = new_version
    locked_message.deleted_at = now
    _append_realtime_event(
        locked_thread=locked_thread,
        kind=ThreadEventKind.MESSAGE_DELETED,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
        message=locked_message,
    )
    locked_message.active_reactions = []  # type: ignore[attr-defined]
    return locked_message


@transaction.atomic
def add_message_reaction(
    *,
    message: Message,
    actor,
    actor_principal_kind: str,
    actor_principal_id: int,
    emoji: str,
) -> Message:
    emoji = _normalize_reaction_emoji(emoji)
    locked_thread, locked_message = _lock_message_for_actor(
        message=message,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )
    if locked_message.deleted_at is not None:
        raise ConflictException(_("A deleted message cannot receive reactions."), code="message_deleted")
    existing = MessageReaction.objects.filter(
        message=locked_message,
        reactor_principal_kind=actor_principal_kind,
        reactor_principal_id=actor_principal_id,
        emoji=emoji,
        removed_at__isnull=True,
    ).first()
    if existing is None:
        MessageReaction.objects.create(
            message=locked_message,
            reactor=actor,
            reactor_principal_kind=actor_principal_kind,
            reactor_principal_id=actor_principal_id,
            emoji=emoji,
        )
        now = timezone.now()
        Thread.objects.filter(pk=locked_thread.pk).update(updated_at=now)
        _append_realtime_event(
            locked_thread=locked_thread,
            kind=ThreadEventKind.REACTION_ADDED,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
            message=locked_message,
        )
    _refresh_active_reactions(locked_message)
    return locked_message


@transaction.atomic
def remove_message_reaction(
    *,
    message: Message,
    actor,
    actor_principal_kind: str,
    actor_principal_id: int,
    emoji: str,
) -> Message:
    emoji = _normalize_reaction_emoji(emoji)
    locked_thread, locked_message = _lock_message_for_actor(
        message=message,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )
    reaction = (
        MessageReaction.objects.select_for_update()
        .filter(
            message=locked_message,
            reactor_principal_kind=actor_principal_kind,
            reactor_principal_id=actor_principal_id,
            emoji=emoji,
            removed_at__isnull=True,
        )
        .first()
    )
    if reaction is not None:
        now = timezone.now()
        MessageReaction.objects.filter(pk=reaction.pk).update(removed_at=now)
        Thread.objects.filter(pk=locked_thread.pk).update(updated_at=now)
        _append_realtime_event(
            locked_thread=locked_thread,
            kind=ThreadEventKind.REACTION_REMOVED,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
            message=locked_message,
        )
    _refresh_active_reactions(locked_message)
    return locked_message


@transaction.atomic
def post_message(
    *,
    thread: Thread,
    sender,
    body: str,
    attachments=None,
    sender_principal_kind: str | None = None,
    sender_principal_id: int | None = None,
) -> Message:
    """Append a message. The sender must already be a participant. Bumps the thread,
    marks the sender caught-up, and notifies the other participants (realtime push
    reuses the notifications fan-out)."""
    caller_thread = thread
    locked_thread = Thread.objects.select_for_update().filter(pk=thread.pk).first()
    if locked_thread is None:
        raise NotFoundException(_("Thread not found."), code="not_found")
    thread = locked_thread
    attachments = [] if attachments is None else attachments
    if not body.strip() and not attachments:
        raise ValidationException(_("A message needs text or an attachment."), code="empty_message")
    if len(body) > _MAX_MESSAGE_BODY_CHARS:
        raise ValidationException(
            _("The message is too long."),
            code="validation_error",
            fields={"body": [_("Use at most 10,000 characters.")]},
        )
    if sender_principal_kind is None or sender_principal_id is None:
        from core.role_principals import resolve_unambiguous_user_principal

        sender_principal = resolve_unambiguous_user_principal(
            sender.id,
            field="sender",
            message=_("The sender does not identify one active role account."),
        )
        sender_principal_kind = sender_principal.kind
        sender_principal_id = sender_principal.principal_id
    sender_participant = ThreadParticipant.objects.select_for_update().filter(
        thread=thread,
        user=sender,
        principal_kind=sender_principal_kind,
        principal_id=sender_principal_id,
        attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
    )
    if not sender_participant.exists():
        raise PermissionException(_("You are not a participant of this thread."), code="not_participant")

    now = timezone.now()
    # The durable object namespace contains the message primary key.  A valid
    # upload policy can therefore only overwrite its staging object, never a
    # message attachment that participants are allowed to download.
    message = Message.objects.create(
        thread=thread,
        sender=sender,
        sender_principal_kind=sender_principal_kind,
        sender_principal_id=sender_principal_id,
        sender_attribution_status=ParticipantAttributionStatus.CAPTURED,
        body=body,
        attachments=[],
    )
    promoted: list[str] = []
    try:
        promoted = _materialize_message_attachments(keys=attachments, actor=sender, message=message)
        message.attachments = promoted
        message.save(update_fields=["attachments"])
        Thread.objects.filter(pk=thread.pk).update(last_message_at=now, updated_at=now)
        thread.last_message_at = now
        thread.updated_at = now
        # ``create_thread`` returns the instance it just created. Keep that
        # caller-owned object synchronized even though the row lock above uses
        # a separately loaded instance.
        caller_thread.last_message_at = now
        caller_thread.updated_at = now
        # The sender has, by definition, read up to their own message.
        sender_participant.update(
            last_read_at=timezone.now(),
            last_read_message=message,
            hidden_at=None,
        )
        # Deleting a chat hides it only for that participant. A new incoming
        # message makes the conversation visible again, matching familiar
        # messaging-app behavior without destroying shared history.
        ThreadParticipant.objects.filter(
            thread=thread,
            attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
        ).exclude(
            user=sender,
            principal_kind=sender_principal_kind,
            principal_id=sender_principal_id,
        ).update(hidden_at=None)
        realtime_event = _append_realtime_event(
            locked_thread=thread,
            kind=ThreadEventKind.MESSAGE_CREATED,
            actor=sender,
            actor_principal_kind=sender_principal_kind,
            actor_principal_id=sender_principal_id,
            message=message,
        )
        caller_thread.realtime_sequence = realtime_event.sequence
        _notify_others(
            thread=thread,
            sender=sender,
            sender_principal_kind=sender_principal_kind,
            sender_principal_id=sender_principal_id,
            message=message,
        )
    except Exception:
        _delete_promoted_objects(promoted)
        raise
    return message


def _notify_others(
    *,
    thread: Thread,
    sender,
    sender_principal_kind: str,
    sender_principal_id: int,
    message: Message,
) -> None:
    from apps.notifications.models import Channel
    from apps.notifications.services import dispatch

    recipients = (
        ThreadParticipant.objects.filter(
            thread=thread,
            attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
        )
        .exclude(
            user=sender,
            principal_kind=sender_principal_kind,
            principal_id=sender_principal_id,
        )
        .values("user_id", "principal_kind", "principal_id", "notifications_muted")
    )
    # Privacy: the notification carries only pointers (thread/message/sender) — never
    # the message body. Content lives once, in the access-scoped thread, so it can't
    # leak through (or be stranded in) a recipient's notification feed.
    for recipient in recipients:
        uid = recipient["user_id"]
        dispatch(
            event_type="message.received",
            recipient_id=uid,
            recipient_principal_kind=recipient["principal_kind"],
            recipient_principal_id=recipient["principal_id"],
            context={
                "thread_id": thread.pk,
                "message_id": message.pk,
                "sender": sender.get_full_name() if sender else "",
            },
            dedupe_key=(f"message:{message.pk}:{recipient['principal_kind']}:{recipient['principal_id']}"),
            channels=([Channel.IN_APP] if recipient["notifications_muted"] else None),
        )


@transaction.atomic
def mark_read(
    *,
    thread: Thread,
    user,
    principal_kind: str,
    principal_id: int,
    through_message_id: int | None = None,
) -> ThreadReadStateDTO:
    """Advance an exact principal's inclusive read cursor monotonically.

    When ``through_message_id`` is supplied, only messages through that exact
    visible row become read—even if a newer message races with the request.  A
    missing id retains the legacy behavior but snapshots the current last
    message under the same thread lock, and the response names that boundary.
    """

    if isinstance(through_message_id, bool) or (
        through_message_id is not None
        and (not isinstance(through_message_id, int) or not 1 <= through_message_id <= _MAX_DATABASE_ID)
    ):
        raise ValidationException(
            _("through_message_id must be a positive integer."),
            code="validation_error",
            fields={"through_message_id": [_("Choose a message from this thread.")]},
        )
    locked_thread = Thread.objects.select_for_update().filter(pk=thread.pk).first()
    if locked_thread is None:
        raise NotFoundException(_("Thread not found."), code="not_found")
    participant = (
        ThreadParticipant.objects.select_for_update()
        .filter(
            thread=locked_thread,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
            attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
        )
        .first()
    )
    if participant is None:
        raise NotFoundException(_("Thread not found."), code="not_found")

    target_query = Message.objects.filter(thread=locked_thread)
    if through_message_id is None:
        through_message = target_query.order_by("-id").first()
    else:
        through_message = target_query.filter(pk=through_message_id).first()
        if through_message is None:
            raise NotFoundException(_("Message not found."), code="not_found")
    if through_message is None:
        # Preserve the legacy observable state for an empty thread without
        # inventing a message cursor or realtime receipt. The first explicit
        # read records that the principal visited the thread; repeats remain
        # idempotent until a message exists.
        if participant.last_read_at is None:
            participant.last_read_at = timezone.now()
            participant.save(update_fields=("last_read_at",))
            return ThreadReadStateDTO(
                changed=True,
                through_message=None,
                read_at=participant.last_read_at,
                event=None,
            )
        return ThreadReadStateDTO(
            changed=False,
            through_message=None,
            read_at=participant.last_read_at,
            event=None,
        )

    current_id = participant.last_read_message_id
    if current_id is not None and current_id >= through_message.pk:
        current_message = Message.objects.filter(pk=current_id, thread=locked_thread).first()
        return ThreadReadStateDTO(
            changed=False,
            through_message=current_message,
            read_at=participant.last_read_at,
            event=None,
        )

    read_at = timezone.now()
    participant.last_read_message = through_message
    participant.last_read_at = read_at
    participant.save(update_fields=("last_read_message", "last_read_at"))
    event = _append_realtime_event(
        locked_thread=locked_thread,
        kind=ThreadEventKind.READ_UPDATED,
        actor=user,
        actor_principal_kind=principal_kind,
        actor_principal_id=principal_id,
        message=through_message,
    )
    return ThreadReadStateDTO(
        changed=True,
        through_message=through_message,
        read_at=read_at,
        event=event,
    )


def set_notifications_muted(
    *, thread: Thread, user, principal_kind: str, principal_id: int, muted: bool
) -> None:
    """Persist the caller's own push preference for one participant thread."""
    ThreadParticipant.objects.filter(
        thread=thread,
        user=user,
        principal_kind=principal_kind,
        principal_id=principal_id,
        attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
    ).update(notifications_muted=muted)


def set_archived(*, thread: Thread, user, principal_kind: str, principal_id: int, archived: bool) -> None:
    """Archive or restore one conversation for the exact signed-in role account."""
    updated = ThreadParticipant.objects.filter(
        thread=thread,
        user=user,
        principal_kind=principal_kind,
        principal_id=principal_id,
        attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
        hidden_at__isnull=True,
    ).update(archived_at=timezone.now() if archived else None)
    if not updated:
        raise NotFoundException(_("Thread not found."), code="not_found")


def hide_thread(*, thread: Thread, user, principal_kind: str, principal_id: int) -> None:
    """Hide a conversation for one participant without deleting shared history."""
    updated = ThreadParticipant.objects.filter(
        thread=thread,
        user=user,
        principal_kind=principal_kind,
        principal_id=principal_id,
        attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
        hidden_at__isnull=True,
    ).update(hidden_at=timezone.now(), archived_at=None)
    if not updated:
        raise NotFoundException(_("Thread not found."), code="not_found")
