"""Schedule domain signals (emit-only today; D3-C notifications consume).

Flat primitive kwargs + ``schema_name`` for cross-context dispatch.
"""

from __future__ import annotations

import django.dispatch

lesson_reminder_due = django.dispatch.Signal()
lesson_cancelled = django.dispatch.Signal()
lesson_rescheduled = django.dispatch.Signal()
# Bulk rule moves deliberately use one aggregate signal.  Re-emitting
# ``lesson_rescheduled`` once per row makes every synchronous receiver repeat its
# cohort-recipient lookup and dispatch loop (lessons x recipients) on the HTTP
# request's commit path.
lessons_bulk_rescheduled = django.dispatch.Signal()
