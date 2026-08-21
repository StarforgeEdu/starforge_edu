"""Session restriction, logout, password-policy, and organization bootstrap contracts."""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django_tenants.utils import schema_context

from config.asgi import application
from core.permissions import Role

pytestmark = pytest.mark.django_db

CHANGE_URL = "/api/v1/auth/password/change/"
LOGOUT_URL = "/api/v1/auth/logout/"
LOGOUT_ALL_URL = "/api/v1/auth/logout-all/"
ME_URL = "/api/v1/users/me/"
TEMPORARY_PASSWORD = "Temporary-Orbit-42"
NEW_PASSWORD = "Permanent-Nebula-77"


def _temporary_student_session(tenant, *, username: str = "temporary.student"):
    from apps.students.tests.factories import StudentProfileFactory
    from core.session_auth import create_session

    with schema_context(tenant.schema_name):
        student = StudentProfileFactory(username=username)
        student.set_password(TEMPORARY_PASSWORD)
        student.must_change_password = True
        student.save(update_fields=("password", "must_change_password"))
        session = create_session(
            student.user,
            principal_kind="student",
            principal_id=student.pk,
        )
        return student.pk, session.key


def _bearer_client(client_for, tenant, access: str):
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


def _password_user(tenant, user_in, as_user, *, username: str | None = None):
    kwargs = {"username": username} if username else {}
    user = user_in(tenant, roles=[Role.TEACHER], **kwargs)
    with schema_context(tenant.schema_name):
        user.set_password(TEMPORARY_PASSWORD)
        user.save(update_fields=("password",))
    return user, as_user(tenant, user)


def test_temporary_password_session_is_centrally_restricted_but_can_recover(
    tenant_a,
    client_for,
):
    student_id, access = _temporary_student_session(tenant_a)
    client = _bearer_client(client_for, tenant_a, access)

    # One layered view and one DRF view prove both stacks share the authenticator gate.
    for business_url in ("/api/v1/users/devices/", "/api/v1/reports/"):
        denied = client.get(business_url)
        assert denied.status_code == 403
        assert denied.json()["code"] == "password_change_required"

    me = client.get(ME_URL)
    assert me.status_code == 200, me.content
    assert me.json()["data"]["must_change_password"] is True

    profile_write = client.patch(
        ME_URL,
        {"preferred_language": "ru"},
        format="json",
    )
    assert profile_write.status_code == 403
    assert profile_write.json()["code"] == "password_change_required"

    changed = client.post(
        CHANGE_URL,
        {"old_password": TEMPORARY_PASSWORD, "new_password": NEW_PASSWORD},
        format="json",
    )
    assert changed.status_code == 200, changed.content
    fresh = _bearer_client(client_for, tenant_a, changed.json()["data"]["access"])
    assert fresh.get("/api/v1/users/devices/").status_code == 200

    with schema_context(tenant_a.schema_name):
        from apps.students.models import StudentProfile

        assert StudentProfile.objects.get(pk=student_id).must_change_password is False


@pytest.mark.parametrize("url", [LOGOUT_URL, LOGOUT_ALL_URL])
def test_temporary_password_session_can_end_session(url, tenant_a, client_for):
    _student_id, access = _temporary_student_session(
        tenant_a,
        username=f"temporary-{url.rstrip('/').rsplit('/', 1)[-1]}",
    )
    response = _bearer_client(client_for, tenant_a, access).post(url, {}, format="json")
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_logout_is_current_session_only_and_both_logout_actions_are_idempotent(
    tenant_a,
    user_in,
    client_for,
):
    from apps.users.models import Session
    from core.session_auth import create_session

    user = user_in(tenant_a, roles=[Role.TEACHER])
    original_token_version = user.token_version
    with schema_context(tenant_a.schema_name):
        first = create_session(user)
        second = create_session(user)
        first_id, first_access = first.pk, first.key
        second_id, second_access = second.pk, second.key

    first_client = _bearer_client(client_for, tenant_a, first_access)
    assert first_client.post(LOGOUT_URL).status_code == 200
    assert first_client.post(LOGOUT_URL).status_code == 200

    second_client = _bearer_client(client_for, tenant_a, second_access)
    assert second_client.get(ME_URL).status_code == 200
    with schema_context(tenant_a.schema_name):
        assert Session.objects.get(pk=first_id).revoked_at is not None
        assert Session.objects.get(pk=second_id).revoked_at is None
        user.refresh_from_db()
        assert user.token_version == original_token_version

    assert second_client.post(LOGOUT_ALL_URL).status_code == 200
    assert second_client.post(LOGOUT_ALL_URL).status_code == 200
    with schema_context(tenant_a.schema_name):
        assert Session.objects.get(pk=second_id).revoked_at is not None
        user.refresh_from_db()
        assert user.token_version == original_token_version + 1


def test_wrong_password_has_stable_field_error(tenant_a, user_in, as_user):
    _user, client = _password_user(tenant_a, user_in, as_user)
    response = client.post(
        CHANGE_URL,
        {"old_password": "incorrect-password", "new_password": NEW_PASSWORD},
        format="json",
    )
    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "code": "wrong_password",
        "message": "The current password is incorrect.",
        "errors": {"old_password": ["The current password is incorrect."]},
    }


@pytest.mark.parametrize(
    ("new_password", "message_fragment"),
    [
        ("A7!xxxxxx", "at least 10"),
        ("A7!" + "x" * 126, "no more than 128"),
        ("A7!" + "x" * 1022, "no more than 128"),
        ("password123", "too common"),
        ("1234567890", "entirely numeric"),
    ],
)
def test_weak_passwords_use_structured_validator_errors(
    new_password,
    message_fragment,
    tenant_a,
    user_in,
    as_user,
):
    _user, client = _password_user(tenant_a, user_in, as_user)
    response = client.post(
        CHANGE_URL,
        {"old_password": TEMPORARY_PASSWORD, "new_password": new_password},
        format="json",
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "weak_password"
    assert body["message"] == "Choose a stronger password."
    assert any(message_fragment in message for message in body["errors"]["new_password"])


def test_similarity_validator_uses_the_authenticated_account(tenant_a, user_in, as_user):
    _user, client = _password_user(
        tenant_a,
        user_in,
        as_user,
        username="executive.leader",
    )
    response = client.post(
        CHANGE_URL,
        {"old_password": TEMPORARY_PASSWORD, "new_password": "executive.leader-2026"},
        format="json",
    )
    assert response.status_code == 400
    assert any("too similar" in message for message in response.json()["errors"]["new_password"])


@pytest.mark.parametrize("new_password", ["A7!xxxxxxx", "A7!" + "x" * 125])
def test_password_length_boundaries_10_and_128_are_accepted(
    new_password,
    tenant_a,
    user_in,
    as_user,
):
    _user, client = _password_user(tenant_a, user_in, as_user)
    response = client.post(
        CHANGE_URL,
        {"old_password": TEMPORARY_PASSWORD, "new_password": new_password},
        format="json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["data"]["access"]


def test_me_returns_authoritative_timezone_and_read_only_session(
    tenant_a,
    user_in,
    client_for,
):
    from apps.org.models import CenterSettings
    from core.session_auth import create_session

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        center_settings = CenterSettings.load()
        center_settings.organization_timezone = "Europe/Paris"
        center_settings.save(update_fields=("organization_timezone",))
        session = create_session(user, read_only=True)
        access = session.key

    response = _bearer_client(client_for, tenant_a, access).get(ME_URL)
    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["organization_timezone"] == "Europe/Paris"
    assert data["read_only_session"] is True


def test_center_settings_validates_iana_timezone(tenant_a, as_role):
    client, _user = as_role(Role.DIRECTOR)

    valid = client.patch(
        "/api/v1/org/settings/",
        {"organization_timezone": "America/New_York"},
        format="json",
    )
    assert valid.status_code == 200
    assert valid.json()["data"]["organization_timezone"] == "America/New_York"

    invalid = client.patch(
        "/api/v1/org/settings/",
        {"organization_timezone": "Mars/Olympus_Mons"},
        format="json",
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "validation_error"
    assert "organization_timezone" in invalid.json()["errors"]


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_temporary_password_session_cannot_open_websocket(tenant_a):
    _student_id, access = await sync_to_async(_temporary_student_session)(tenant_a)
    communicator = WebsocketCommunicator(
        application,
        f"/ws/notifications/?token={access}",
        headers=[(b"host", b"a.localhost")],
    )
    connected, code = await communicator.connect()
    assert connected is False
    assert code == 4401
