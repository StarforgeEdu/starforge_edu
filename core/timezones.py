"""Organization business-time context for tenant requests and tasks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_tenants.utils import get_public_schema_name

from core.exceptions import ServiceUnavailableException
from core.utils import current_schema


@contextmanager
def organization_timezone_context() -> Iterator[None]:
    """Activate the tenant's authoritative IANA timezone for one execution.

    The public schema has no organization settings and retains Django's default
    timezone. Tenant configuration is validated on write, but the defensive
    lookup below also fails closed if legacy/manual database changes introduced
    an invalid value. ``timezone.override`` restores the prior context even when
    a view or task raises, preventing cross-request/task timezone leakage in a
    reused worker thread.
    """

    if current_schema() == get_public_schema_name():
        yield
        return

    from apps.org.selectors import get_center_settings

    timezone_name = get_center_settings().organization_timezone
    try:
        business_timezone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ServiceUnavailableException(
            _("Organization time settings are unavailable."),
            code="configuration_unavailable",
        ) from exc

    with timezone.override(business_timezone):
        yield
