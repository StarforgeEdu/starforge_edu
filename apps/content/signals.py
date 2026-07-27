"""Content domain signals (emit-only).

Consumers: D4-A AI content summary listens to ``file_upload_confirmed`` to
generate a summary for the just-confirmed file. Flat primitive kwargs +
``schema_name`` for cross-context Celery dispatch.

Signatures:
    file_upload_confirmed.send(sender=LessonFile, file_id, requested_by, schema_name)
"""

from __future__ import annotations

import django.dispatch

file_upload_confirmed = django.dispatch.Signal()
