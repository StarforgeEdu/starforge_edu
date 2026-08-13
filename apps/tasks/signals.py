"""Task-domain events consumed by notifications and other workflow listeners."""

from __future__ import annotations

import django.dispatch

task_assigned = django.dispatch.Signal()
