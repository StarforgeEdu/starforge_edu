"""AI API fixtures that exercise production-shaped account sessions."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from tests.role_principal_helpers import ensure_role_principal, exact_session_client


@pytest.fixture
def as_role(as_role, client_for):
    """Replace the legacy bridge-token helper with one exact role session.

    AI inputs and outputs are principal-private, so these tests must not rely on
    the temporary test-only principal-union adapter.  Keeping this override in
    the AI test package avoids changing unrelated domain fixtures while making
    every AI API assertion match the production authentication boundary.
    """

    legacy_as_role = as_role

    def _make(role, tenant=None):
        # The root fixture accepts an omitted tenant, but AI tests always pass
        # one explicitly.  Fail clearly if a future test does not.
        if tenant is None:
            raise AssertionError("AI as_role calls must name their tenant explicitly.")
        _legacy_client, user = legacy_as_role(role, tenant)
        with schema_context(tenant.schema_name):
            membership = user.role_memberships.select_related("branch").get()
            ensure_role_principal(user, roles=[role], branch=membership.branch)
        return exact_session_client(client_for, tenant, user), user

    return _make
