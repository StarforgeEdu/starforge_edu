"""Messaging service — participant-scoped reads + participant resolution, wrapping the
preserved domain fns (create_thread / post_message / mark_read)."""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.messaging.dto.thread_dto import CreateThreadDTO, ThreadEventPageDTO, ThreadReadStateDTO
from apps.messaging.interfaces.repositories import IThreadRepository
from apps.messaging.interfaces.services import IThreadService
from apps.messaging.models import Message, Thread
from apps.messaging.services import (
    add_message_reaction,
    assert_thread_safeguarding,
    create_thread,
    delete_message,
    edit_message,
    hide_thread,
    mark_read,
    post_message,
    remove_message_reaction,
    set_archived,
    set_notifications_muted,
)
from apps.users.models import User
from core.exceptions import PermissionException, ValidationException
from core.permissions import get_user_roles_for_user, has_permission_code
from core.role_principals import (
    RolePrincipal,
    request_role_principal,
    resolve_unambiguous_user_principals,
)

_MAX_DATABASE_ID = 9_223_372_036_854_775_807


class ThreadService(IThreadService):
    def __init__(self, repository: IThreadRepository) -> None:
        self.repository = repository

    def scoped_threads(self, *, user, principal_kind: str, principal_id: int) -> QuerySet[Thread]:
        return self.repository.participant_threads(
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )

    def get_thread(self, *, user, principal_kind: str, principal_id: int, pk: int) -> Thread | None:
        return self.repository.get_participant_thread(
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
            pk=pk,
        )

    def messages_of(self, *, thread: Thread) -> QuerySet[Message]:
        return self.repository.messages_of(thread=thread)

    def get_message(
        self,
        *,
        user,
        principal_kind: str,
        principal_id: int,
        pk: int,
    ) -> Message | None:
        return self.repository.get_participant_message(
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
            pk=pk,
        )

    def event_page(
        self,
        *,
        thread: Thread,
        after: int,
        limit: int,
    ) -> ThreadEventPageDTO:
        if isinstance(after, bool) or not isinstance(after, int) or not 0 <= after <= _MAX_DATABASE_ID:
            raise ValidationException(
                _("after must be a non-negative event cursor."),
                code="validation_error",
                fields={"after": [_("Use zero or a previously returned event cursor.")]},
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValidationException(
                _("limit must be between 1 and 100."),
                code="validation_error",
                fields={"limit": [_("Choose a value from 1 through 100.")]},
            )
        page = self.repository.event_page(thread=thread, after=after, limit=limit)
        if after > page.high_watermark:
            raise ValidationException(
                _("The event cursor is ahead of this thread."),
                code="invalid_event_cursor",
                fields={"after": [_("Use a cursor returned for this thread.")]},
            )
        return page

    def can_stream_thread(
        self,
        *,
        thread_id: int,
        user,
        principal_kind: str,
        principal_id: int,
    ) -> bool:
        """Revalidate one exact role principal, permission, and thread seat."""

        from core.exceptions import ValidationException as PrincipalValidationException
        from core.role_principals import validate_role_principal

        try:
            validate_role_principal(
                kind=principal_kind,
                principal_id=principal_id,
                user_id=user.pk,
                field="principal",
            )
        except PrincipalValidationException:
            return False
        if not user.is_superuser:
            roles = get_user_roles_for_user(
                user,
                principal_kind=principal_kind,
                principal_id=principal_id,
                principal_validated=True,
            )
            if not has_permission_code(roles, "messaging:read"):
                return False
        return self.repository.is_participant(
            thread_id=thread_id,
            user_id=user.pk,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )

    def unread_counts(
        self,
        *,
        thread_ids: list[int],
        viewer_id: int,
        viewer_principal_kind: str,
        viewer_principal_id: int,
    ) -> dict[int, int]:
        return self.repository.unread_counts(
            thread_ids=thread_ids,
            viewer_id=viewer_id,
            viewer_principal_kind=viewer_principal_kind,
            viewer_principal_id=viewer_principal_id,
        )

    def contacts(self, *, authorization_context, category: str = "") -> QuerySet[User]:
        return self.repository.contacts_for(
            authorization_context=authorization_context,
            category=category,
        )

    def create(self, data: CreateThreadDTO, *, authorization_context) -> Thread:
        creator = authorization_context.user
        creator_principal = request_role_principal(
            authorization_context,
            error_code="messaging_principal_unavailable",
        )
        requested_others = set(data.participant_ids) - {creator.pk}
        users = self.repository.active_members(ids=data.participant_ids)
        if len(users) != len(data.participant_ids):
            raise ValidationException(
                _("One or more participants were not found."), code="unknown_participant"
            )
        other_users = [user for user in users if user.pk in requested_others]
        try:
            principal_by_user: dict[int, RolePrincipal] = resolve_unambiguous_user_principals(
                requested_others,
                field="participant_ids",
                message=_("One or more participants do not identify one active role account."),
            )
        except ValidationException as exc:
            # The bridge User and its role-profile topology are private
            # implementation details. A guessed tenant user id must not become
            # an oracle for missing or ambiguous role-native accounts.
            raise PermissionException(
                _("One or more recipients are outside your messaging scope."),
                code="recipient_out_of_scope",
            ) from exc
        requested_principals = list(principal_by_user.values())
        expected_scope = {
            (principal.user_id, principal.kind, principal.principal_id) for principal in requested_principals
        }
        if (
            self.repository.recipient_scope_principals(
                authorization_context=authorization_context,
                principals=requested_principals,
            )
            != expected_scope
        ):
            raise PermissionException(
                _("One or more recipients are outside your messaging scope."),
                code="recipient_out_of_scope",
            )
        allowed = set(
            self.repository.contacts_for(authorization_context=authorization_context)
            .filter(pk__in=requested_others)
            .values_list("pk", flat=True)
        )
        if allowed != requested_others:
            # Geographic scope is known to be valid at this point. Preserve the
            # stronger safeguarding reason for a same-scope non-staff pairing,
            # while relationship-ineligible recipients remain fail-closed.
            assert_thread_safeguarding(
                creator=creator,
                participants=other_users,
                authorization_context=authorization_context,
                creator_principal=creator_principal,
                participant_principals=principal_by_user,
            )
            raise PermissionException(
                _("One or more recipients are outside your messaging scope."),
                code="recipient_out_of_scope",
            )
        return create_thread(
            creator=creator,
            participants=other_users,
            subject=data.subject,
            first_body=data.first_body,
            attachments=data.attachments,
            authorization_context=authorization_context,
            creator_principal=creator_principal,
            participant_principals=principal_by_user,
        )

    def post(
        self,
        *,
        thread: Thread,
        sender,
        sender_principal_kind: str,
        sender_principal_id: int,
        body: str,
        attachments: list,
    ) -> Message:
        return post_message(
            thread=thread,
            sender=sender,
            sender_principal_kind=sender_principal_kind,
            sender_principal_id=sender_principal_id,
            body=body,
            attachments=attachments,
        )

    def edit_message(
        self,
        *,
        message: Message,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
        body: str,
        expected_version: int | None,
    ) -> Message:
        return edit_message(
            message=message,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
            body=body,
            expected_version=expected_version,
        )

    def delete_message(
        self,
        *,
        message: Message,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
    ) -> Message:
        return delete_message(
            message=message,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
        )

    def add_reaction(
        self,
        *,
        message: Message,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
        emoji: str,
    ) -> Message:
        return add_message_reaction(
            message=message,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
            emoji=emoji,
        )

    def remove_reaction(
        self,
        *,
        message: Message,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
        emoji: str,
    ) -> Message:
        return remove_message_reaction(
            message=message,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
            emoji=emoji,
        )

    def mark_read(
        self,
        *,
        thread: Thread,
        user,
        principal_kind: str,
        principal_id: int,
        through_message_id: int | None,
    ) -> ThreadReadStateDTO:
        return mark_read(
            thread=thread,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
            through_message_id=through_message_id,
        )

    def set_notifications_muted(
        self,
        *,
        thread: Thread,
        user,
        principal_kind: str,
        principal_id: int,
        muted: bool,
    ) -> None:
        set_notifications_muted(
            thread=thread,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
            muted=muted,
        )

    def set_archived(
        self,
        *,
        thread: Thread,
        user,
        principal_kind: str,
        principal_id: int,
        archived: bool,
    ) -> None:
        set_archived(
            thread=thread,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
            archived=archived,
        )

    def hide_thread(self, *, thread: Thread, user, principal_kind: str, principal_id: int) -> None:
        hide_thread(
            thread=thread,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )

    def presign_attachment(self, *, filename: str, content_type: str, size_bytes: int, requested_by) -> dict:
        from apps.messaging.services import presign_attachment_upload

        return presign_attachment_upload(
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            requested_by=requested_by,
        )

    def attachment_download_url(self, *, thread: Thread, key: str) -> str:
        from apps.messaging.services import attachment_download_url

        return attachment_download_url(thread=thread, key=key)
