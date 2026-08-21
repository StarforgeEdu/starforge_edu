"""Exact-principal authenticated-session register regressions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db

SESSIONS = "/api/v1/users/sessions/"


def _session_fixture(tenant):
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import Device
    from core.session_auth import create_session
    from tests.role_principal_helpers import shared_staff_teacher_bridge

    with schema_context(tenant.schema_name):
        branch = BranchFactory()
        user, teacher, staff = shared_staff_teacher_bridge(
            branch=branch,
            staff_role="director",
        )
        current = create_session(
            user,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
            ),
            device_id="current-browser",
            principal_kind="staff",
            principal_id=staff.pk,
        )
        other_staff = create_session(
            user,
            user_agent="Mozilla/5.0 (iPhone) Version/17.0 Mobile Safari/604.1",
            device_id="phone-browser",
            principal_kind="staff",
            principal_id=staff.pk,
        )
        other_role = create_session(
            user,
            user_agent="secret-teacher-user-agent",
            device_id="teacher-device",
            principal_kind="teacher",
            principal_id=teacher.pk,
        )
        Device.objects.create(
            user=user,
            device_id="current-browser",
            platform=Device.PLATFORM_WEB,
            user_agent="raw-device-fingerprint",
        )
        expired = create_session(
            user,
            principal_kind="staff",
            principal_id=staff.pk,
        )
        type(expired).objects.filter(pk=expired.pk).update(
            expires_at=expired.created_at - timedelta(seconds=1),
        )
        return user, staff, current, other_staff, other_role, expired


def _client(client_for, tenant, access):
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


def test_session_register_is_exact_principal_scoped_and_privacy_minimized(tenant_a, client_for):
    _user, _staff, current, other_staff, _other_role, _expired = _session_fixture(tenant_a)
    response = _client(client_for, tenant_a, current.key).get(SESSIONS)

    assert response.status_code == 200, response.content
    rows = response.json()["data"]
    assert {row["id"] for row in rows} == {current.pk, other_staff.pk}
    assert sum(row["current_session"] for row in rows) == 1
    current_row = next(row for row in rows if row["current_session"])
    assert current_row["platform"] == "web"
    assert current_row["device"] == "Windows"
    assert current_row["browser"] == "Chrome"
    serialized = response.content.decode()
    for secret in (
        current.key,
        "raw-device-fingerprint",
        "secret-teacher-user-agent",
        "current-browser",
    ):
        assert secret not in serialized


def test_session_revoke_is_exact_principal_scoped(tenant_a, client_for):
    _user, _staff, current, other_staff, other_role, _expired = _session_fixture(tenant_a)
    client = _client(client_for, tenant_a, current.key)

    hidden = client.delete(f"{SESSIONS}{other_role.pk}/")
    assert hidden.status_code == 404
    revoked = client.delete(f"{SESSIONS}{other_staff.pk}/")
    assert revoked.status_code == 204

    with schema_context(tenant_a.schema_name):
        other_role.refresh_from_db()
        other_staff.refresh_from_db()
        assert other_role.revoked_at is None
        assert other_staff.revoked_at is not None


def test_session_register_rejects_unknown_duplicate_and_oversized_queries(tenant_a, client_for):
    _user, _staff, current, *_rest = _session_fixture(tenant_a)
    client = _client(client_for, tenant_a, current.key)

    assert client.get(f"{SESSIONS}?seach=phone").status_code == 400
    assert client.get(f"{SESSIONS}?page=1&page=2").status_code == 400
    assert client.get(f"{SESSIONS}?page_size=101").status_code == 400


def test_read_only_session_cannot_revoke_sessions(tenant_a, client_for):
    from core.session_auth import create_session

    user, staff, _current, other_staff, *_rest = _session_fixture(tenant_a)
    with schema_context(tenant_a.schema_name):
        restricted = create_session(
            user,
            principal_kind="staff",
            principal_id=staff.pk,
            read_only=True,
        )
    response = _client(client_for, tenant_a, restricted.key).delete(f"{SESSIONS}{other_staff.pk}/")
    assert response.status_code == 403


def test_revoking_current_bearer_session_ends_it_immediately(tenant_a, client_for):
    _user, _staff, current, *_rest = _session_fixture(tenant_a)
    client = _client(client_for, tenant_a, current.key)

    assert client.delete(f"{SESSIONS}{current.pk}/").status_code == 204
    assert client.get("/api/v1/users/me/").status_code == 401
