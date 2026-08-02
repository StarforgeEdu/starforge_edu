"""Object-storage lifecycle hooks for assignment records."""

from __future__ import annotations

import logging

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.assignments.models import Assignment, Submission
from apps.assignments.services import enqueue_attachment_deletions, trusted_attachment_keys

logger = logging.getLogger(__name__)


def _queue_trusted_keys(instance: Assignment | Submission) -> None:
    keys = trusted_attachment_keys(instance)
    if keys:
        enqueue_attachment_deletions(list(keys))
    elif instance.attachments:
        logger.warning(
            "Skipped assignment attachment cleanup for untrusted references model=%s id=%s",
            instance._meta.label,
            instance.pk,
        )


@receiver(pre_delete, sender=Assignment, dispatch_uid="assignments.delete_assignment_objects")
def delete_assignment_objects(sender, instance: Assignment, **_kwargs) -> None:
    _queue_trusted_keys(instance)


@receiver(pre_delete, sender=Submission, dispatch_uid="assignments.delete_submission_objects")
def delete_submission_objects(sender, instance: Submission, **_kwargs) -> None:
    _queue_trusted_keys(instance)
