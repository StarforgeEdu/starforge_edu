"""Messaging response presenters (the DRF Thread/Message serializer shape)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.messaging.dto.thread_dto import ThreadEventPageDTO, ThreadReadStateDTO
from apps.messaging.models import Message, Thread, ThreadParticipant, ThreadRealtimeEvent
from core.permissions import Role


def participant_to_dict(participant: ThreadParticipant) -> dict:
    return {
        "user": participant.user_id,
        "principal_kind": participant.principal_kind,
        "principal_id": participant.principal_id,
        "last_read_at": participant.last_read_at.isoformat() if participant.last_read_at else None,
        "last_read_message_id": participant.last_read_message_id,
        "added_at": participant.added_at.isoformat(),
    }


def thread_to_dict(
    thread: Thread,
    *,
    unread_count: int,
    viewer_id: int | None = None,
    viewer_principal_kind: str = "",
    viewer_principal_id: int | None = None,
) -> dict:
    # unread_count is supplied by the caller (computed in one bounded query via
    # ThreadService.unread_counts) rather than derived from a prefetch of every message.
    viewer_participant = next(
        (
            participant
            for participant in thread.participants.all()
            if participant.user_id == viewer_id
            and participant.principal_kind == viewer_principal_kind
            and participant.principal_id == viewer_principal_id
        ),
        None,
    )
    return {
        "id": thread.id,
        "subject": thread.subject,
        "branch": thread.branch_id,
        "created_by": thread.created_by_id,
        "last_message_at": thread.last_message_at.isoformat() if thread.last_message_at else None,
        "realtime_cursor": thread.realtime_sequence,
        "realtime_protocol": "starforge.messaging.thread.v1",
        "created_at": thread.created_at.isoformat(),
        "participants": [participant_to_dict(p) for p in thread.participants.all()],
        "unread_count": unread_count,
        "notifications_muted": bool(viewer_participant and viewer_participant.notifications_muted),
    }


def message_to_dict(message: Message) -> dict:
    return {
        "id": message.id,
        "thread": message.thread_id,
        "sender": message.sender_id,
        "sender_principal_kind": message.sender_principal_kind,
        "sender_principal_id": message.sender_principal_id,
        "sender_attribution_status": message.sender_attribution_status,
        "body": message.body,
        "attachments": message.attachments,
        "created_at": message.created_at.isoformat(),
    }


def thread_event_to_dict(event: ThreadRealtimeEvent) -> dict:
    """Pointer-only event; message content is fetched from the scoped REST list."""

    return {
        "thread_id": event.thread_id,
        "sequence": event.sequence,
        "kind": event.kind,
        "message_id": event.message_id,
        "actor_principal_kind": event.actor_principal_kind,
        "actor_principal_id": event.actor_principal_id,
        "created_at": event.created_at.isoformat(),
    }


def thread_event_page_to_dict(page: ThreadEventPageDTO, *, thread_id: int) -> dict:
    return {
        "thread_id": thread_id,
        "events": [thread_event_to_dict(event) for event in page.events],
        "requested_after": page.requested_after,
        "next_cursor": page.next_cursor,
        "high_watermark": page.high_watermark,
        "recovery_floor": page.recovery_floor,
        "has_more": page.has_more,
        "reset_required": page.reset_required,
    }


def thread_read_state_to_dict(state: ThreadReadStateDTO, *, thread_id: int) -> dict:
    return {
        "thread_id": thread_id,
        "changed": state.changed,
        "through_message_id": state.through_message.pk if state.through_message is not None else None,
        "read_at": state.read_at.isoformat() if state.read_at is not None else None,
        "event_cursor": state.event.sequence if state.event is not None else None,
    }


def contact_to_dict(user) -> dict:
    """Safe messaging recipient summary backed by a real bridge User id."""
    teacher = getattr(user, "teacher_profile", None)
    staff = getattr(user, "staff_profile", None)
    student = getattr(user, "student_profile", None)
    parent = getattr(user, "parent_profile", None)
    if getattr(user, "contact_is_staff", False) and teacher is not None and teacher.is_active:
        principal_kind, profile = "teacher", teacher
    elif getattr(user, "contact_is_staff", False) and staff is not None and staff.is_active:
        principal_kind, profile = "staff", staff
    elif getattr(user, "contact_is_parent", False) and parent is not None and parent.is_active:
        principal_kind, profile = "parent", parent
    else:
        principal_kind, profile = "student", student

    memberships = getattr(user, "messaging_memberships", ())

    def membership_matches(membership) -> bool:
        account_type = membership.account_type
        if account_type is not None:
            return account_type.account_kind == principal_kind
        if principal_kind == "teacher":
            return membership.role == Role.TEACHER
        if principal_kind == "student":
            return membership.role == Role.STUDENT
        if principal_kind == "parent":
            return membership.role == Role.PARENT
        return membership.role not in (Role.TEACHER, Role.STUDENT, Role.PARENT)

    membership = next((m for m in memberships if membership_matches(m)), None)
    if membership is None:
        membership = next(iter(memberships), None)
    if membership is not None and membership.account_type is not None:
        role_label = membership.account_type.name
        role_slug = membership.account_type.slug
    else:
        role_slug = membership.role if membership is not None else principal_kind
        role_label = role_slug.replace("_", " ").title()

    display_name = profile.get_full_name() if profile is not None else ""
    username = (profile.username if profile is not None else "") or user.username
    last_seen = user.last_seen_at
    recently_active = bool(last_seen and last_seen >= timezone.now() - timedelta(minutes=5))
    return {
        # Keep `id` as a compatibility alias while making the bridge semantics explicit.
        "id": user.pk,
        "user_id": user.pk,
        "principal_kind": principal_kind,
        "category": principal_kind if principal_kind in ("student", "parent") else "staff",
        "profile_id": profile.pk if profile is not None else None,
        "display_name": display_name or username,
        "username": username,
        "role_label": role_label,
        "role_slug": role_slug,
        # Deprecated compatibility hint.  It is explicitly false as a presence
        # contract; the current realtime protocol does not publish presence.
        "is_online": recently_active,
        "is_online_is_heuristic": True,
        "recently_active": recently_active,
        "presence_source": "last_seen_within_5_minutes",
        "activity_status": "recently_active" if recently_active else "not_recently_active",
        "activity_status_is_presence": False,
    }
