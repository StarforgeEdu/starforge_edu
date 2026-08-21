"""The authenticated principal fast path is both query-free and unforgeable."""

from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django_tenants.utils import schema_context

from core.exceptions import PermissionException
from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_session_validated_principal_is_reused_without_role_table_query(
    tenant_a,
    user_in,
    django_assert_num_queries,
):
    from apps.audit.context import bind_request, reset_request
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from core.role_principals import request_role_principal
    from core.session_auth import SessionAuthentication, create_session

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        user = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)
        profile = StaffProfile.objects.create(user=user, username="fast.path.staff")
        session = create_session(
            user,
            principal_kind="staff",
            principal_id=profile.pk,
        )
        request = RequestFactory().get(
            "/api/v1/messaging/threads/",
            HTTP_AUTHORIZATION=f"Bearer {session.key}",
        )
        tokens = bind_request(request)
        try:
            authenticated = SessionAuthentication().authenticate(request)
            assert authenticated is not None
            request.user, request.auth = authenticated

            # Authentication already checked active profile ownership. Domain helpers
            # reuse the opaque server marker instead of repeating that same SELECT.
            with django_assert_num_queries(0):
                principal = request_role_principal(request)
        finally:
            reset_request(tokens)
        assert (principal.kind, principal.principal_id, principal.user_id) == (
            "staff",
            profile.pk,
            user.pk,
        )


def test_public_validated_boolean_cannot_forge_principal(tenant_a, monkeypatch):
    from apps.users.tests.factories import UserFactory
    from core import role_principals

    calls = []

    def reject(**kwargs):
        calls.append(kwargs)
        return False

    with schema_context(tenant_a.schema_name):
        user = UserFactory()
        forged = SimpleNamespace(
            user=user,
            principal_kind="staff",
            principal_id=987654,
            # This historical/public hint is not the authenticator's opaque marker.
            principal_validated=True,
        )
        monkeypatch.setattr(role_principals, "_profile_exists", reject)
        with pytest.raises(PermissionException):
            role_principals.request_role_principal(forged)
    assert calls == [{"kind": "staff", "principal_id": 987654, "user_id": user.pk}]
