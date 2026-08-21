"""Cohort domain signals.

`cohort_member_moved` fires when a student is moved between cohorts. Day-1
consumer is log-only; Day 3 Lane D (audit, TD-9) attaches an AuditLog receiver.
"""

from __future__ import annotations

import django.dispatch

cohort_member_moved = django.dispatch.Signal()
