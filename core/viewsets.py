"""Tenant-safe base views.

Every tenant-scoped view must assert the active connection is on a tenant
schema (not public). `TenantSafeModelViewSet` covers CRUD; `TenantSafeAPIView`
covers non-CRUD APIViews (settings endpoint, custom actions). This guards
against accidentally serving tenant data through the public URLConf.
"""

from __future__ import annotations

from django.db import connection
from django_tenants.utils import get_public_schema_name
from rest_framework import viewsets
from rest_framework.views import APIView

from core.exceptions import PermissionException, TenantContextMissing
from core.permissions import (
    SAFE_METHODS,
    DenyWriteForReadOnlyToken,
    ObjectScopedPermission,
    Role,
    RolePermission,
    get_role_memberships,
    is_read_only_token,
)


def assert_tenant_context() -> None:
    schema = getattr(connection, "schema_name", None)
    if not schema or schema == get_public_schema_name():
        raise TenantContextMissing()


def assert_not_read_only_write(request) -> None:
    """D4-LE-4 (hardened): a read-only impersonation token may only make SAFE
    requests. Enforced here in ``initial`` — NOT only via a permission class — so a
    subclass that overrides ``permission_classes`` (many TenantSafeAPIViews do)
    can never accidentally let a read-only token through on a write."""
    if request.method not in SAFE_METHODS and is_read_only_token(request):
        raise PermissionException(code="read_only_token")


class TenantSafeModelViewSet(viewsets.ModelViewSet):
    permission_classes = [RolePermission, ObjectScopedPermission, DenyWriteForReadOnlyToken]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        assert_tenant_context()
        assert_not_read_only_write(request)

    def perform_create(self, serializer):
        # ObjectScopedPermission only runs via has_object_permission (detail
        # routes) — create has no object yet, so a branch/department-scoped role
        # could otherwise create rows in ANY branch. Enforce the same scope on
        # the incoming branch/department here.
        self._assert_create_scope(serializer)
        serializer.save()

    def _assert_create_scope(self, serializer) -> None:
        scope = getattr(self, "object_scope", None)
        if not scope:
            return
        user = self.request.user
        if getattr(user, "is_superuser", False):
            return
        memberships = get_role_memberships(self.request)
        if any(m.role == Role.DIRECTOR for m in memberships):
            return
        target = serializer.validated_data.get(scope)
        if target is None:
            return
        target_id = getattr(target, "pk", target)
        allowed = {getattr(m, f"{scope}_id") for m in memberships}
        if target_id not in allowed:
            raise PermissionException(code="out_of_scope")


class TenantSafeAPIView(APIView):
    # Fail closed by default (TD-4): a subclass that forgets to declare
    # permission_classes must NOT inherit DRF's permissive IsAuthenticated.
    # Self-scoped views (e.g. the iCal feed) override this explicitly.
    permission_classes = [RolePermission]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        assert_tenant_context()
        assert_not_read_only_write(request)
