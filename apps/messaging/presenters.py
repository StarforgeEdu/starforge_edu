"""Messaging response presenters (the DRF Thread/Message serializer shape)."""

from __future__ import annotations

from apps.messaging.models import Message, Thread, ThreadParticipant


def participant_to_dict(participant: ThreadParticipant) -> dict:
    return {
        "user": participant.user_id,
        "last_read_at": participant.last_read_at.isoformat() if participant.last_read_at else None,
        "added_at": participant.added_at.isoformat(),
    }


def thread_to_dict(thread: Thread, *, unread_count: int) -> dict:
    # unread_count is supplied by the caller (computed in one bounded query via
    # ThreadService.unread_counts) rather than derived from a prefetch of every message.
    return {
        "id": thread.id,
        "subject": thread.subject,
        "branch": thread.branch_id,
        "created_by": thread.created_by_id,
        "last_message_at": thread.last_message_at.isoformat() if thread.last_message_at else None,
        "created_at": thread.created_at.isoformat(),
        "participants": [participant_to_dict(p) for p in thread.participants.all()],
        "unread_count": unread_count,
    }


def message_to_dict(message: Message) -> dict:
    return {
        "id": message.id,
        "thread": message.thread_id,
        "sender": message.sender_id,
        "body": message.body,
        "attachments": message.attachments,
        "created_at": message.created_at.isoformat(),
    }
