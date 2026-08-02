"""In-app messaging (F4-4 / D-6): threads, participants, messages.

A `Thread` is a conversation between a set of `ThreadParticipant`s (e.g. a student
and one or more teachers). `Message`s are append-only (accountability DNA — a
conversation record can't be quietly rewritten). `ThreadParticipant.last_read_at`
drives unread counts.
"""

from __future__ import annotations

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class ParticipantPrincipalKind(models.TextChoices):
    STUDENT = "student", _("Student")
    TEACHER = "teacher", _("Teacher")
    PARENT = "parent", _("Parent")
    STAFF = "staff", _("Staff")


class ParticipantAttributionStatus(models.TextChoices):
    CAPTURED = "captured", _("Captured at write time")
    RESOLVED = "resolved", _("Resolved by reviewed backfill")
    UNRESOLVED = "unresolved", _("Unresolved")
    CONFLICTING = "conflicting", _("Conflicting evidence")
    QUARANTINED = "quarantined", _("Quarantined for review")


DELIVERABLE_PARTICIPANT_STATUSES = (
    ParticipantAttributionStatus.CAPTURED,
    ParticipantAttributionStatus.RESOLVED,
)

_PARTICIPANT_SNAPSHOT_FIELDS = (
    "user_id",
    "principal_kind",
    "principal_id",
    "attribution_status",
)

_MESSAGE_SNAPSHOT_FIELDS = (
    "sender_principal_kind",
    "sender_principal_id",
    "sender_attribution_status",
)


def _participant_candidates(user_id: int) -> list[tuple[str, int]]:
    labels = {
        ParticipantPrincipalKind.STUDENT: "students.StudentProfile",
        ParticipantPrincipalKind.TEACHER: "teachers.TeacherProfile",
        ParticipantPrincipalKind.PARENT: "parents.ParentProfile",
        ParticipantPrincipalKind.STAFF: "org.StaffProfile",
    }
    matches: list[tuple[str, int]] = []
    for kind, label in labels.items():
        model = django_apps.get_model(label)
        principal_id = (
            model.objects.filter(user_id=user_id, user__is_active=True, is_active=True)
            .values_list("pk", flat=True)
            .first()
        )
        if principal_id is not None:
            matches.append((str(kind), int(principal_id)))
    return matches


def _capture_new_participant_snapshot(instance) -> None:
    """Capture one provable role account or leave the participant fail-closed."""

    if not instance._state.adding or instance.user_id is None:
        return
    if instance.principal_kind is not None or instance.principal_id is not None:
        if instance.principal_kind is None or instance.principal_id is None:
            instance.principal_kind = None
            instance.principal_id = None
            instance.attribution_status = ParticipantAttributionStatus.QUARANTINED
            return
        from core.exceptions import ValidationException
        from core.role_principals import validate_role_principal

        try:
            principal = validate_role_principal(
                kind=instance.principal_kind,
                principal_id=instance.principal_id,
                user_id=instance.user_id,
                field="participant",
            )
        except ValidationException:
            instance.principal_kind = None
            instance.principal_id = None
            instance.attribution_status = ParticipantAttributionStatus.QUARANTINED
            return
        instance.principal_kind = principal.kind
        instance.principal_id = principal.principal_id
        instance.attribution_status = ParticipantAttributionStatus.CAPTURED
        return

    candidates = _participant_candidates(instance.user_id)
    if len(candidates) == 1:
        instance.principal_kind, instance.principal_id = candidates[0]
        instance.attribution_status = ParticipantAttributionStatus.CAPTURED
    elif len(candidates) > 1:
        instance.attribution_status = ParticipantAttributionStatus.CONFLICTING
    else:
        instance.attribution_status = ParticipantAttributionStatus.UNRESOLVED


def _guard_immutable_participant_snapshot(instance, *, update_fields=None) -> None:
    if instance._state.adding or instance.pk is None:
        return
    if update_fields is not None and set(map(str, update_fields)).isdisjoint(_PARTICIPANT_SNAPSHOT_FIELDS):
        return
    previous = (
        type(instance)._default_manager.filter(pk=instance.pk).values(*_PARTICIPANT_SNAPSHOT_FIELDS).first()
    )
    if previous is None:
        return
    changed = [field for field in _PARTICIPANT_SNAPSHOT_FIELDS if previous[field] != getattr(instance, field)]
    if changed:
        raise ValidationError(
            {field: [str(_("Messaging participant attribution is immutable."))] for field in changed}
        )


def _capture_new_message_snapshot(instance) -> None:
    """Capture one provable sender principal or quarantine the attribution.

    Normal messaging writes pass the exact session principal.  Direct ORM/admin
    imports remain fail-closed: a shared bridge user is never guessed when it
    owns more than one active role account.
    """

    if not instance._state.adding or instance.sender_id is None:
        return
    if instance.sender_principal_kind is not None or instance.sender_principal_id is not None:
        if instance.sender_principal_kind is None or instance.sender_principal_id is None:
            instance.sender_principal_kind = None
            instance.sender_principal_id = None
            instance.sender_attribution_status = ParticipantAttributionStatus.QUARANTINED
            return
        from core.exceptions import ValidationException
        from core.role_principals import validate_role_principal

        try:
            principal = validate_role_principal(
                kind=instance.sender_principal_kind,
                principal_id=instance.sender_principal_id,
                user_id=instance.sender_id,
                field="sender",
            )
        except ValidationException:
            instance.sender_principal_kind = None
            instance.sender_principal_id = None
            instance.sender_attribution_status = ParticipantAttributionStatus.QUARANTINED
            return
        instance.sender_principal_kind = principal.kind
        instance.sender_principal_id = principal.principal_id
        instance.sender_attribution_status = ParticipantAttributionStatus.CAPTURED
        return

    # A message belongs to a conversation seat, not merely to one of the role
    # accounts linked to the bridge user.  Infer only from the exact thread;
    # direct/imported rows without a provable seat remain quarantined from
    # principal-specific realtime attribution.
    candidates = list(
        ThreadParticipant.objects.filter(
            thread_id=instance.thread_id,
            user_id=instance.sender_id,
            attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
        ).values_list("principal_kind", "principal_id")[:2]
    )
    if len(candidates) == 1:
        instance.sender_principal_kind, instance.sender_principal_id = candidates[0]
        instance.sender_attribution_status = ParticipantAttributionStatus.CAPTURED
    elif len(candidates) > 1:
        instance.sender_attribution_status = ParticipantAttributionStatus.CONFLICTING
    else:
        instance.sender_attribution_status = ParticipantAttributionStatus.UNRESOLVED


def _guard_immutable_message_snapshot(instance, *, update_fields=None) -> None:
    if instance._state.adding or instance.pk is None:
        return
    if update_fields is not None and set(map(str, update_fields)).isdisjoint(_MESSAGE_SNAPSHOT_FIELDS):
        return
    previous = (
        type(instance)._default_manager.filter(pk=instance.pk).values(*_MESSAGE_SNAPSHOT_FIELDS).first()
    )
    if previous is None:
        return
    changed = [field for field in _MESSAGE_SNAPSHOT_FIELDS if previous[field] != getattr(instance, field)]
    if changed:
        raise ValidationError(
            {field: [str(_("Message sender attribution is immutable."))] for field in changed}
        )


class MessageAttachmentUploadGrant(models.Model):
    """Single-use, owner-bound authorization for a messaging S3 object."""

    key = models.CharField(max_length=512, unique=True)
    requested_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    content_type = models.CharField(max_length=127)
    expected_size_bytes = models.PositiveBigIntegerField()
    actual_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    source_deleted_at = models.DateTimeField(null=True, blank=True)
    durable_key = models.CharField(max_length=512, null=True, blank=True, unique=True)
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    durable_deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("requested_by", "consumed_at", "expires_at")),
            models.Index(
                fields=("source_deleted_at", "expires_at"),
                name="message_upload_source_exp_idx",
            ),
            models.Index(
                fields=("durable_deleted_at", "deletion_requested_at"),
                name="message_upload_delete_idx",
            ),
        ]


class Thread(models.Model):
    subject = models.CharField(max_length=200, blank=True)
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="threads"
    )
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Allocated while this row is locked.  It is the durable, per-thread cursor
    # used by WebSocket delivery and missed-event recovery; Redis is never the
    # source of ordering truth.
    realtime_sequence = models.PositiveBigIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-last_message_at", "-created_at")

    def __str__(self) -> str:  # pragma: no cover
        # Subjects may contain sensitive conversation context. Keep them out of
        # implicit admin/log formatting and require an explicitly scoped field
        # read wherever the product really needs the value.
        return f"thread#{self.pk}"


class ThreadParticipant(models.Model):
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="thread_participations")
    principal_kind = models.CharField(
        max_length=16,
        choices=ParticipantPrincipalKind.choices,
        null=True,
        blank=True,
        editable=False,
    )
    principal_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    attribution_status = models.CharField(
        max_length=12,
        choices=ParticipantAttributionStatus.choices,
        default=ParticipantAttributionStatus.UNRESOLVED,
        db_default=ParticipantAttributionStatus.UNRESOLVED,
        editable=False,
    )
    last_read_at = models.DateTimeField(null=True, blank=True)
    last_read_message = models.ForeignKey(
        "messaging.Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    notifications_muted = models.BooleanField(default=False)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Message.sender and unread accounting intentionally retain the bridge
            # User FK.  Never let two role principals backed by that same bridge
            # occupy separate seats in one thread, or sender attribution would be
            # ambiguous inside that conversation.
            models.UniqueConstraint(
                fields=("thread", "user"),
                name="one_participation_per_user_per_thread",
            ),
            models.UniqueConstraint(
                fields=("thread", "principal_kind", "principal_id"),
                condition=models.Q(attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES),
                name="one_participation_per_thread_principal",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
                        principal_kind__in=ParticipantPrincipalKind.values,
                        principal_id__isnull=False,
                    )
                    | models.Q(
                        attribution_status__in=(
                            ParticipantAttributionStatus.UNRESOLVED,
                            ParticipantAttributionStatus.CONFLICTING,
                            ParticipantAttributionStatus.QUARANTINED,
                        ),
                        principal_kind__isnull=True,
                        principal_id__isnull=True,
                    )
                ),
                name="message_participant_attribution_shape",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "thread")),
            models.Index(
                fields=("principal_kind", "principal_id", "thread"),
                name="msg_part_principal_idx",
            ),
            models.Index(
                fields=("attribution_status", "thread"),
                name="message_participant_review_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return (
            f"participant:{self.principal_kind or 'unresolved'}:"
            f"{self.principal_id or self.user_id}@thread#{self.thread_id}"
        )

    def save(self, *args, **kwargs) -> None:
        _capture_new_participant_snapshot(self)
        _guard_immutable_participant_snapshot(self, update_fields=kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    @property
    def is_deliverable(self) -> bool:
        return self.attribution_status in DELIVERABLE_PARTICIPANT_STATUSES


class Message(models.Model):
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="sent_messages"
    )
    sender_principal_kind = models.CharField(
        max_length=16,
        choices=ParticipantPrincipalKind.choices,
        null=True,
        blank=True,
        editable=False,
    )
    sender_principal_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    sender_attribution_status = models.CharField(
        max_length=12,
        choices=ParticipantAttributionStatus.choices,
        default=ParticipantAttributionStatus.UNRESOLVED,
        db_default=ParticipantAttributionStatus.UNRESOLVED,
        editable=False,
    )
    body = models.TextField()
    attachments = models.JSONField(default=list, blank=True)  # S3 keys
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        # id is the tiebreaker so same-millisecond messages keep a stable order
        # (deterministic pagination, no skipped/duplicated rows).
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=("thread", "created_at")),
            models.Index(fields=("thread", "id"), name="message_thread_cursor_idx"),
            models.Index(
                fields=("sender_attribution_status", "thread"),
                name="message_sender_review_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        sender_attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
                        sender_principal_kind__in=ParticipantPrincipalKind.values,
                        sender_principal_id__isnull=False,
                    )
                    | models.Q(
                        sender_attribution_status__in=(
                            ParticipantAttributionStatus.UNRESOLVED,
                            ParticipantAttributionStatus.CONFLICTING,
                            ParticipantAttributionStatus.QUARANTINED,
                        ),
                        sender_principal_kind__isnull=True,
                        sender_principal_id__isnull=True,
                    )
                ),
                name="message_sender_attribution_shape",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"message#{self.pk}@thread#{self.thread_id}"

    def save(self, *args, **kwargs) -> None:
        _capture_new_message_snapshot(self)
        _guard_immutable_message_snapshot(self, update_fields=kwargs.get("update_fields"))
        super().save(*args, **kwargs)


class ThreadEventKind(models.TextChoices):
    MESSAGE_CREATED = "message.created", _("Message created")
    READ_UPDATED = "read.updated", _("Read state updated")


class ThreadRealtimeEvent(models.Model):
    """Append-only pointer event for one thread's recoverable realtime stream.

    The channel layer carries only this durable row's identifiers.  Message
    content remains in the participant-scoped REST resource and therefore never
    enters Redis group payloads or WebSocket infrastructure logs.
    """

    thread = models.ForeignKey(Thread, on_delete=models.PROTECT, related_name="realtime_events")
    sequence = models.PositiveBigIntegerField()
    kind = models.CharField(max_length=32, choices=ThreadEventKind.choices)
    actor = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    actor_principal_kind = models.CharField(
        max_length=16,
        choices=ParticipantPrincipalKind.choices,
        editable=False,
    )
    actor_principal_id = models.PositiveBigIntegerField(editable=False)
    # For message.created this is the created message; for read.updated it is
    # the exact inclusive message through which the actor has read.
    message = models.ForeignKey(Message, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("thread_id", "sequence")
        constraints = [
            models.UniqueConstraint(
                fields=("thread", "sequence"),
                name="one_realtime_sequence_per_thread",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gt=0),
                name="messaging_realtime_sequence_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(actor_principal_kind__in=ParticipantPrincipalKind.values)
                    & models.Q(actor_principal_id__gt=0)
                ),
                name="messaging_realtime_actor_shape",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"thread-event:{self.thread_id}:{self.sequence}:{self.kind}"

    def save(self, *args, **kwargs) -> None:
        if not self._state.adding:
            raise ValidationError(_("Messaging realtime events are append-only."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Messaging realtime events are append-only."))
