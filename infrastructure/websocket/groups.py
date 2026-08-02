"""Canonical, injection-safe Channels group names for tenant realtime feeds."""

from __future__ import annotations

import re
from typing import Any, cast

from django_tenants.utils import get_public_schema_name

_TENANT_SCHEMA_RE = re.compile(r"\A[a-z][a-z0-9_]{0,62}\Z")
_PRINCIPAL_KINDS = frozenset({"student", "teacher", "parent", "staff"})


def _tenant_schema(value: object) -> str:
    schema = str(value or "")
    if not _TENANT_SCHEMA_RE.fullmatch(schema) or schema == get_public_schema_name():
        raise ValueError("A tenant schema is required for a realtime group.")
    return schema


def _positive_id(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("A positive integer id is required for a realtime group.")
    try:
        object_id = int(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise ValueError("A positive integer id is required for a realtime group.") from exc
    if object_id <= 0 or str(object_id) != str(value):
        raise ValueError("A positive integer id is required for a realtime group.")
    return object_id


def user_group(schema: object, user_id: object) -> str:
    """Return a legacy bridge-user group (never used for private notifications)."""

    return f"{_tenant_schema(schema)}.user.{_positive_id(user_id)}"


def notification_principal_group(schema: object, principal_kind: object, principal_id: object) -> str:
    """Return an exact tenant + role-native notification group."""

    kind = str(principal_kind or "")
    if kind not in _PRINCIPAL_KINDS:
        raise ValueError("A role-native principal kind is required for a notification group.")
    # Channels caps group names at 100 characters. PostgreSQL tenant schemas may
    # be 63 characters, so the deliberately short ``.n.`` namespace keeps even
    # a 20-digit bigint principal below that hard limit.
    return f"{_tenant_schema(schema)}.n.{kind}.{_positive_id(principal_id)}"


def messaging_thread_group(schema: object, thread_id: object) -> str:
    """Return one tenant-private messaging thread group.

    Only opaque numeric identifiers enter the name.  Subjects, participant
    names, message bodies, and attachment keys therefore cannot leak through
    Redis keys, channel-layer errors, or infrastructure telemetry.
    """

    return f"{_tenant_schema(schema)}.m.t.{_positive_id(thread_id)}"


def cohort_attendance_group(schema: object, cohort_id: object) -> str:
    """Return the tenant-private attendance group for ``cohort_id``."""

    return f"{_tenant_schema(schema)}.cohort.{_positive_id(cohort_id)}"
