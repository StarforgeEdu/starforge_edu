from __future__ import annotations

from contextlib import contextmanager

import pytest
from django.test import RequestFactory, override_settings
from django_tenants.utils import schema_context

from core.permissions import Role, get_user_roles, has_permission_code
from core.session_auth import SessionAuthentication, create_session

pytestmark = pytest.mark.django_db


@contextmanager
def _authenticated_request(raw_key: str):
    from apps.audit.context import bind_request, reset_request

    request = RequestFactory().get(
        "/api/v1/teachers/",
        HTTP_AUTHORIZATION=f"Bearer {raw_key}",
    )
    tokens = bind_request(request)
    try:
        result = SessionAuthentication().authenticate(request)
        assert result is not None
        request.user, request.auth = result
        yield request
    finally:
        reset_request(tokens)


@override_settings(
    ALLOW_LEGACY_TENANT_SESSIONS_FOR_TESTS=True,
    ALLOW_LEGACY_PRINCIPAL_UNION_FOR_TESTS=True,
)
def test_blank_test_session_can_use_explicit_legacy_principal_adapter(tenant_a, user_in):
    user = user_in(tenant_a, roles=[Role.DIRECTOR])

    with schema_context(tenant_a.schema_name):
        session = create_session(user)
        with _authenticated_request(session.key) as request:
            roles = get_user_roles(request)
            assert request._allow_legacy_principal_union_for_tests is True
            assert has_permission_code(roles, "teachers:write")


@override_settings(
    ALLOW_LEGACY_TENANT_SESSIONS_FOR_TESTS=True,
    ALLOW_LEGACY_PRINCIPAL_UNION_FOR_TESTS=False,
)
def test_blank_session_fails_closed_when_legacy_principal_adapter_is_disabled(tenant_a, user_in):
    user = user_in(tenant_a, roles=[Role.DIRECTOR])

    with schema_context(tenant_a.schema_name):
        session = create_session(user)
        with _authenticated_request(session.key) as request:
            roles = get_user_roles(request)
            assert not hasattr(request, "_allow_legacy_principal_union_for_tests")
            assert not roles
            assert not has_permission_code(roles, "teachers:write")


@override_settings(
    ALLOW_LEGACY_TENANT_SESSIONS_FOR_TESTS=True,
    ALLOW_LEGACY_PRINCIPAL_UNION_FOR_TESTS=True,
)
def test_principal_bound_session_never_enables_legacy_membership_union(tenant_a):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = StudentProfileFactory(branch=branch)
        RoleMembership.objects.create(user=student.user, branch=branch, role=Role.STUDENT)
        RoleMembership.objects.create(user=student.user, branch=branch, role=Role.DIRECTOR)
        session = create_session(
            student.user,
            principal_kind="student",
            principal_id=student.pk,
        )
        with _authenticated_request(session.key) as request:
            roles = get_user_roles(request)
            assert not hasattr(request, "_allow_legacy_principal_union_for_tests")
            assert has_permission_code(roles, "students:read")
            assert not has_permission_code(roles, "access:read")
