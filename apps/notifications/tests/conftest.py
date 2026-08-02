"""Notification tests use role-native accounts, never a bare bridge User."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.notifications.tests.helpers import ensure_notification_principal


@pytest.fixture
def user_in(user_in):
    """Extend the project fixture with one canonical notification principal."""

    base_user_in = user_in

    def _make(tenant, *, roles=(), branch=None, **kwargs):
        user = base_user_in(tenant, roles=roles, branch=branch, **kwargs)
        role_values = {str(role) for role in roles}
        if "student" in role_values:
            kind = "student"
        elif "teacher" in role_values:
            kind = "teacher"
        elif "parent" in role_values:
            kind = "parent"
        else:
            kind = "staff"
        with schema_context(tenant.schema_name):
            membership = user.role_memberships.filter(revoked_at__isnull=True).first()
            profile_branch = branch or (membership.branch if membership is not None else None)
            ensure_notification_principal(user, kind=kind, branch=profile_branch)
        return user

    return _make


@pytest.fixture
def as_user(client_for):
    """Authenticate notification requests as the fixture's exact role account."""

    def _make(tenant, user):
        from core.session_auth import create_session

        with schema_context(tenant.schema_name):
            session = create_session(
                user,
                principal_kind=user.notification_principal_kind,
                principal_id=user.notification_principal_id,
            )
        client = client_for(tenant)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
        return client

    return _make
