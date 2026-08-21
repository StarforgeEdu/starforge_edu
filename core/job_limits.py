"""Concurrency-safe admission controls for expensive tenant background jobs.

The application is PostgreSQL-only (schema-per-tenant).  A transaction-scoped
advisory lock gives every web process and worker the same tenant-wide admission
gate without adding a public-schema coordination table.  Callers must already be
inside ``transaction.atomic()``.
"""

from __future__ import annotations

from django.db import connection

from core.utils import current_schema


def lock_tenant_job_queue(namespace: str = "documents") -> None:
    """Serialize job admission for ``namespace`` in the current tenant.

    ``pg_advisory_xact_lock`` is automatically released on commit/rollback, so a
    crashed request cannot leave the queue locked.  The schema is part of the key
    to keep unrelated centers independent.
    """

    key = f"starforge:{current_schema()}:{namespace}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])


def try_acquire_job_execution(namespace: str, object_id: int) -> bool:
    """Acquire a session lock for one expensive job without waiting."""
    key = f"starforge:{current_schema()}:{namespace}:{object_id}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [key])
        return bool(cursor.fetchone()[0])


def release_job_execution(namespace: str, object_id: int) -> None:
    """Release a lock obtained by :func:`try_acquire_job_execution`."""
    key = f"starforge:{current_schema()}:{namespace}:{object_id}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", [key])
