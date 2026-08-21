"""Access-config repository port. Overrides are an UNSCOPED per-tenant table
(the whole centre's permission config; only access:read/write — director-only by
default — can see or change it)."""

from __future__ import annotations

from apps.access.models import RolePermissionOverride
from core.interfaces import IBaseRepository


class IOverrideRepository(IBaseRepository[RolePermissionOverride]): ...
