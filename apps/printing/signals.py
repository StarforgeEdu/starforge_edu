"""Printing signals (emit-only, D4-LD).

Past-tense names; emitted from services via ``transaction.on_commit`` with flat
primitive payloads + ``schema_name`` so cross-context receivers can re-activate
the tenant schema. ``apps.notifications`` consumes ``print_job_failed`` (the
final-failure fan-out goes through ``dispatch()`` only — never an adapter call
from this app).
"""

from __future__ import annotations

import django.dispatch

# kwargs: job_id, source, source_id, branch_id, schema_name
print_job_created = django.dispatch.Signal()

# kwargs: job_id, requested_by_id, source, source_id, schema_name
print_job_failed = django.dispatch.Signal()
