"""Periodic cleanup tasks. Wired via django-celery-beat / CELERY_BEAT_SCHEDULE."""

from __future__ import annotations

from django.utils import timezone
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.users.models import OTP
from config.celery import app


def _all_schemas() -> list[str]:
    """Public schema + every tenant schema (tables in SHARED+TENANT apps live in
    all of them, so a single public delete would miss every tenant's rows)."""
    Tenant = get_tenant_model()
    return [get_public_schema_name(), *Tenant.objects.values_list("schema_name", flat=True)]


@app.task
def purge_expired_otps() -> int:
    """Purge expired OTP rows from the public schema AND every tenant schema.

    `users_otp` exists in all schemas (apps.users is SHARED + TENANT, TD-3), so a
    single public-schema delete would leak every tenant's expired rows. Iterate
    explicitly. (D4-F may convert this to a true per-tenant fan-out at scale.)
    """
    total = 0
    for schema in _all_schemas():
        with schema_context(schema):
            deleted, _ = OTP.objects.filter(expires_at__lt=timezone.now()).delete()
            total += deleted
    return total


@app.task
def flush_expired_jwt_blacklist() -> int:
    """Delete expired simplejwt blacklist/outstanding rows in every schema.

    Wraps simplejwt's ``flushexpiredtokens``: ``OutstandingToken`` /
    ``BlacklistedToken`` exist in the public schema AND every tenant schema
    (``token_blacklist`` is in both SHARED_APPS and TENANT_APPS, TD-3), so we
    delete per-schema rather than calling the command once. Delete-by-filter on
    ``expires_at__lte=now`` — naturally idempotent (a second run finds nothing).
    Returns the number of OutstandingToken rows removed (BlacklistedToken rows
    cascade with their parent).
    """
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    total = 0
    for schema in _all_schemas():
        with schema_context(schema):
            deleted, _ = OutstandingToken.objects.filter(expires_at__lte=timezone.now()).delete()
            total += deleted
    return total
