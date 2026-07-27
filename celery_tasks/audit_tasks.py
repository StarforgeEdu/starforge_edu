"""Audit retention beat task (D3-D-6).

Fan-out per active Center (weekly). Per schema, delete `AuditLog` rows past their
retention window:

- **7 years** for financial / grade resource types (`RETENTION_LONG_TYPES`):
  finance.Invoice, payments.Payment, finance.Refund, academics.Grade,
  academics.ExamResult.
- **1 year** for everything else (logins, OTP events, exports, ProviderConfig,
  users.User / users.RoleMembership, ...).

Idempotent by nature: deleting by age is convergent — a re-run on the same day
deletes nothing new. Returns the number of rows deleted (per-schema task) /
schemas fanned out (dispatcher).
"""

from __future__ import annotations

from config.celery import app

# Resource types kept for the long (7-year) statutory window. Stored as the
# "<app_label>.<Model>" label the receivers write into AuditLog.resource_type.
RETENTION_LONG_TYPES: tuple[str, ...] = (
    "finance.Invoice",
    "payments.Payment",
    "finance.Refund",
    "academics.Grade",
    "academics.ExamResult",
)

RETENTION_LONG_DAYS = 365 * 7  # 7 years
RETENTION_SHORT_DAYS = 365  # 1 year


def _active_schemas() -> list[str]:
    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center

    # Exclude the public Center: AuditLog is TENANT_APPS-only and absent in the
    # public schema, so cleaning it there raises ProgrammingError.
    return list(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .values_list("schema_name", flat=True)
    )


@app.task
def cleanup_old_audit_logs() -> int:
    """Public dispatcher: fan the retention sweep out to each active Center."""
    schemas = _active_schemas()
    for schema in schemas:
        cleanup_old_audit_logs_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task
def cleanup_old_audit_logs_for_schema() -> int:
    """Per-tenant retention sweep. Returns rows deleted in this schema."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.audit.models import AuditLog

    now = timezone.now()
    long_cutoff = now - timedelta(days=RETENTION_LONG_DAYS)
    short_cutoff = now - timedelta(days=RETENTION_SHORT_DAYS)

    # Long-retention rows older than 7y.
    long_deleted, _ = AuditLog.objects.filter(
        resource_type__in=RETENTION_LONG_TYPES, created_at__lt=long_cutoff
    ).delete()
    # Everything else older than 1y.
    short_deleted, _ = (
        AuditLog.objects.filter(created_at__lt=short_cutoff)
        .exclude(resource_type__in=RETENTION_LONG_TYPES)
        .delete()
    )
    return int(long_deleted) + int(short_deleted)
