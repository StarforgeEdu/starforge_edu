"""Fail-closed tenant-schema guard for tenant-only API operations."""

from django.db import connection
from django_tenants.utils import get_public_schema_name

from core.exceptions import TenantContextMissing


def assert_tenant_context() -> None:
    schema = getattr(connection, "schema_name", None)
    if not schema or schema == get_public_schema_name():
        raise TenantContextMissing()
