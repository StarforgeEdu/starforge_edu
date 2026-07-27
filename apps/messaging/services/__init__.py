"""Messaging services (F4-4).

Preserved verbatim; the layered service (services/v1/thread_service.py) wraps
`create_thread` / `post_message` / `mark_read` after the view validates the body and
resolves participants. `post_message` fans out realtime notifications via
apps.notifications.dispatch (pointers only, never the body).
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.messaging.models import Message, Thread, ThreadParticipant
from core.exceptions import PermissionException, ValidationException
from core.permissions import Role

_NON_STAFF = {Role.STUDENT, Role.PARENT}


def _roles_of(user) -> set[str]:
    return {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}


def _is_staff(user) -> bool:
    return bool(_roles_of(user) - _NON_STAFF)


@transaction.atomic
def create_thread(
    *, creator, participants: list, subject: str = "", first_body: str = "", attachments=None
) -> Thread:
    """Open a thread between the creator and `participants` (User objects).

    Safeguarding (dignity DNA): a non-staff opener (student/parent) may only message
    STAFF — never another student/parent — so the channel can't be used for
    unsupervised student-to-student contact. Staff may message anyone.
    """
    members = list({creator.id: creator, **{u.id: u for u in participants}}.values())  # dedup, incl. creator
    others = [u for u in members if u.id != creator.id]
    if not others:
        raise ValidationException(
            _("A thread needs at least one other participant."), code="thread_needs_participant"
        )
    # Safeguarding (dignity DNA) enforced on the resulting PARTICIPANT SET, not just
    # the opener's role: at most one student in any thread (no unsupervised peer
    # channel, even one opened by a teacher), and a non-staff opener may only reach
    # staff (a student/parent can't initiate contact with another non-staff person).
    if sum(1 for u in members if Role.STUDENT in _roles_of(u)) > 1:
        raise PermissionException(
            _("A conversation can include at most one student."), code="non_staff_recipient"
        )
    if not _is_staff(creator) and any(not _is_staff(u) for u in others):
        raise PermissionException(_("You can only message staff."), code="non_staff_recipient")

    thread = Thread.objects.create(subject=subject, created_by=creator, branch=_creator_branch(creator))
    ThreadParticipant.objects.bulk_create([ThreadParticipant(thread=thread, user=u) for u in members])

    if first_body.strip() or attachments:
        post_message(thread=thread, sender=creator, body=first_body, attachments=attachments)
    return thread


def _creator_branch(creator):
    membership = creator.role_memberships.filter(revoked_at__isnull=True, branch__isnull=False).first()
    return membership.branch if membership else None


@transaction.atomic
def post_message(*, thread: Thread, sender, body: str, attachments=None) -> Message:
    """Append a message. The sender must already be a participant. Bumps the thread,
    marks the sender caught-up, and notifies the other participants (realtime push
    reuses the notifications fan-out)."""
    attachments = attachments or []
    if not body.strip() and not attachments:
        raise ValidationException(_("A message needs text or an attachment."), code="empty_message")
    if not ThreadParticipant.objects.filter(thread=thread, user=sender).exists():
        raise PermissionException(_("You are not a participant of this thread."), code="not_participant")

    now = timezone.now()
    message = Message.objects.create(thread=thread, sender=sender, body=body, attachments=attachments)
    Thread.objects.filter(pk=thread.pk).update(last_message_at=now, updated_at=now)
    # The sender has, by definition, read up to their own message.
    ThreadParticipant.objects.filter(thread=thread, user=sender).update(last_read_at=now)
    _notify_others(thread=thread, sender=sender, message=message)
    return message


def _notify_others(*, thread: Thread, sender, message: Message) -> None:
    from apps.notifications.services import dispatch

    recipient_ids = (
        ThreadParticipant.objects.filter(thread=thread).exclude(user=sender).values_list("user_id", flat=True)
    )
    # Privacy: the notification carries only pointers (thread/message/sender) — never
    # the message body. Content lives once, in the access-scoped thread, so it can't
    # leak through (or be stranded in) a recipient's notification feed.
    for uid in recipient_ids:
        dispatch(
            event_type="message.received",
            recipient_id=uid,
            context={
                "thread_id": thread.pk,
                "message_id": message.pk,
                "sender": sender.get_full_name() if sender else "",
            },
            dedupe_key=f"message:{message.pk}:{uid}",
        )


def mark_read(*, thread: Thread, user) -> None:
    ThreadParticipant.objects.filter(thread=thread, user=user).update(last_read_at=timezone.now())
