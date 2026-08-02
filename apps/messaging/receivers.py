"""Committed object cleanup for append-only message attachments."""

from __future__ import annotations

import logging

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.messaging.models import Message
from apps.messaging.services import enqueue_attachment_deletions, trusted_message_attachment_keys

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender=Message, dispatch_uid="messaging.delete_message_objects")
def delete_message_objects(sender, instance: Message, **_kwargs) -> None:
    keys = trusted_message_attachment_keys(instance)
    if keys:
        enqueue_attachment_deletions(list(keys))
    elif instance.attachments:
        logger.warning(
            "Skipped messaging attachment cleanup for untrusted references message_id=%s",
            instance.pk,
        )
