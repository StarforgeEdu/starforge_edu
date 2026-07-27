"""In-app messaging (F4-4 / D-6): threads, participants, messages.

A `Thread` is a conversation between a set of `ThreadParticipant`s (e.g. a student
and one or more teachers). `Message`s are append-only (accountability DNA — a
conversation record can't be quietly rewritten). `ThreadParticipant.last_read_at`
drives unread counts.
"""

from __future__ import annotations

from django.db import models


class Thread(models.Model):
    subject = models.CharField(max_length=200, blank=True)
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="threads"
    )
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-last_message_at", "-created_at")

    def __str__(self) -> str:  # pragma: no cover
        return f"thread#{self.pk}:{self.subject or '(no subject)'}"


class ThreadParticipant(models.Model):
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="thread_participations")
    last_read_at = models.DateTimeField(null=True, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("thread", "user"), name="one_participation_per_user_per_thread"),
        ]
        indexes = [models.Index(fields=("user", "thread"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"participant:{self.user_id}@thread#{self.thread_id}"


class Message(models.Model):
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="sent_messages"
    )
    body = models.TextField()
    attachments = models.JSONField(default=list, blank=True)  # S3 keys
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        # id is the tiebreaker so same-millisecond messages keep a stable order
        # (deterministic pagination, no skipped/duplicated rows).
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=("thread", "created_at"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"message#{self.pk}@thread#{self.thread_id}"
