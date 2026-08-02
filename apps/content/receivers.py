"""Storage lifecycle hooks for content objects.

Database cascades remove ``LessonFile`` rows when a library, course, module,
lesson, or folder is deleted.  Object storage has no foreign keys, so mirror
each committed row deletion asynchronously.  The task is idempotent and keys
are tenant-prefix checked again before deletion.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.content.models import LessonFile
from apps.content.storage_keys import trusted_file_keys
from core.utils import current_schema

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=LessonFile, dispatch_uid="content.delete_lesson_file_objects")
def delete_lesson_file_objects_after_commit(sender, instance: LessonFile, **_kwargs) -> None:
    """Queue deletion of a file's primary object and thumbnail after commit."""

    schema = current_schema()
    keys = trusted_file_keys(instance, schema=schema)
    if not keys:
        if instance.s3_key or instance.thumbnail_key:
            logger.warning("Skipped content cleanup for untrusted object references schema=%s", schema)
        return

    def enqueue() -> None:
        from celery_tasks.content_tasks import delete_content_objects

        delete_content_objects.delay(list(keys), _schema_name=schema)

    transaction.on_commit(enqueue)
