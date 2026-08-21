"""Task tests run through exact role-native sessions."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from tests.role_principal_helpers import ensure_role_principal, exact_session_client


@pytest.fixture
def user_in(user_in):
    base_user_in = user_in

    def _make(tenant, *, roles=(), branch=None, **kwargs):
        user = base_user_in(tenant, roles=roles, branch=branch, **kwargs)
        if roles:
            with schema_context(tenant.schema_name):
                membership = user.role_memberships.filter(revoked_at__isnull=True).first()
                ensure_role_principal(
                    user,
                    roles=roles,
                    branch=branch or (membership.branch if membership is not None else None),
                )
        return user

    return _make


@pytest.fixture
def as_user(client_for):
    return lambda tenant, user: exact_session_client(client_for, tenant, user)
