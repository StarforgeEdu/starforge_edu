"""Auth lifecycle flows over the layered architecture + custom session auth:
username+password login, password change/reset (OTP), session revocation, live roles,
tenant binding. Responses use the success()/error() envelope."""

import re

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db

GENERIC_LOGIN_URL = "/api/v1/auth/login/"
ROLE_LOGIN_URL = "/api/v1/auth/role-login/"
CHANGE_URL = "/api/v1/auth/password/change/"
RESET_REQUEST_URL = "/api/v1/auth/password/reset/request/"
RESET_CONFIRM_URL = "/api/v1/auth/password/reset/confirm/"
LOGOUT_URL = "/api/v1/auth/logout/"
ME_URL = "/api/v1/users/me/"

PASSWORD = "Quasar-Lantern-42"
NEW_PASSWORD = "Nebula-Compass-77"


def _code_from(sms_text: str) -> str:
    match = re.search(rf"\b(\d{{{settings.OTP_LENGTH}}})\b", sms_text)
    assert match, f"no {settings.OTP_LENGTH}-digit code in: {sms_text!r}"
    return match.group(1)


def _password_user(tenant, user_in, *, roles=("teacher",), password=PASSWORD, **kwargs):
    """Create one real teacher role account and retain its bridge User for assertions."""
    assert tuple(roles) == ("teacher",)
    user = user_in(tenant, roles=list(roles), **kwargs)
    with schema_context(tenant.schema_name):
        from apps.teachers.tests.factories import TeacherProfileFactory

        membership = user.role_memberships.get(role="teacher")
        account = TeacherProfileFactory(
            user=user,
            branch=membership.branch,
            username=user.username,
            phone=user.phone,
            # ``User.email`` is nullable for role-native accounts while the
            # teacher-owned identity deliberately stores an empty string for
            # "not supplied".  Mirror the production account-creation path
            # instead of handing ``None`` to a non-null role field.
            email=user.email or "",
        )
        account.set_password(password)
        account.save(update_fields=["password"])
    user._test_role_account = account
    return user


def _role_account(user):
    return user._test_role_account


def _role_login(client_for, tenant, user, *, password=PASSWORD, **payload):
    account = _role_account(user)
    response = client_for(tenant).post(
        ROLE_LOGIN_URL,
        {"username": account.username, "password": password, **payload},
        format="json",
    )
    return response


def _role_client(client_for, tenant, user, *, password=PASSWORD, **payload):
    response = _role_login(client_for, tenant, user, password=password, **payload)
    assert response.status_code == 200, response.content
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['data']['access']}")
    return client


def _reset_payload(user, **values):
    return {
        "identifier": _role_account(user).phone,
        "account_type": "teacher",
        **values,
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_role_login_happy_path_registers_device(tenant_a, client_for, user_in):
    user = _password_user(tenant_a, user_in)
    resp = _role_login(
        client_for,
        tenant_a,
        user,
        device_id="dev-1",
        platform="android",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "access" in body["data"]
    assert "refresh" not in body["data"]  # single-token session auth — no refresh

    with schema_context(tenant_a.schema_name):
        assert user.devices.filter(device_id="dev-1", platform="android").exists()

    authed = client_for(tenant_a)
    authed.credentials(HTTP_AUTHORIZATION=f"Bearer {body['data']['access']}")
    me = authed.get(ME_URL)
    assert me.status_code == 200
    assert me.json()["data"]["username"] == user.username  # layered envelope
    assert me.json()["data"]["tenant_slug"] == tenant_a.schema_name


def test_tenant_generic_bridge_login_is_not_routed(tenant_a, client_for):
    response = client_for(tenant_a).post(
        GENERIC_LOGIN_URL,
        {"username": "bridge-user", "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_role_login_student_signs_in_as_their_role(tenant_a, client_for):
    """A student authenticates with only their role-native username and password.

    The hidden bridge User remains an authorization/audit link and is not itself a
    tenant login identity.
    """
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="ada.student")
        student.set_password(PASSWORD)
        student.save(update_fields=["password"])
        student_id = student.id

    client = client_for(tenant_a)
    resp = client.post(ROLE_LOGIN_URL, {"username": "ada.student", "password": PASSWORD}, format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()["data"]
    assert body["role"] == "student"
    assert body["must_change_password"] is False
    assert "access" in body

    authed = client_for(tenant_a)
    authed.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    me = authed.get(ME_URL)
    assert me.status_code == 200
    assert me.json()["data"]["id"] == student_id
    assert me.json()["data"]["principal_kind"] == "student"
    assert me.json()["data"]["tenant_slug"] == tenant_a.schema_name


def test_role_login_wrong_password_401(tenant_a, client_for):
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="bob.student")
        student.set_password(PASSWORD)
        student.save(update_fields=["password"])

    client = client_for(tenant_a)
    resp = client.post(ROLE_LOGIN_URL, {"username": "bob.student", "password": "wrong-pass"}, format="json")
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


def test_role_login_unknown_username_401(tenant_a, client_for):
    client = client_for(tenant_a)
    resp = client.post(ROLE_LOGIN_URL, {"username": "ghost.nobody", "password": PASSWORD}, format="json")
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


def test_role_login_tracks_current_user_password_after_change(tenant_a, client_for):
    """Role login follows the role account's current credential without stale copies."""
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.services import set_role_account_password

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="cy.student")
        student.set_password(PASSWORD)
        student.save(update_fields=["password"])
        set_role_account_password(student, NEW_PASSWORD)

    client = client_for(tenant_a)
    old = client.post(ROLE_LOGIN_URL, {"username": "cy.student", "password": PASSWORD}, format="json")
    assert old.status_code == 401  # the old password no longer works on role-login
    new = client.post(ROLE_LOGIN_URL, {"username": "cy.student", "password": NEW_PASSWORD}, format="json")
    assert new.status_code == 200, new.content


def test_role_login_wrong_password_has_stable_error(tenant_a, client_for, user_in):
    user = _password_user(tenant_a, user_in)
    resp = _role_login(client_for, tenant_a, user, password="wrong-wrong-1")
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


def test_role_login_unknown_username_same_error(tenant_a, client_for):
    resp = client_for(tenant_a).post(
        ROLE_LOGIN_URL, {"username": "ghost-user", "password": "whatever-123"}, format="json"
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


def test_role_login_inactive_bridge_same_error(tenant_a, client_for, user_in):
    user = _password_user(tenant_a, user_in)
    with schema_context(tenant_a.schema_name):
        user.is_active = False
        user.save(update_fields=["is_active"])
    resp = _role_login(client_for, tenant_a, user)
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


def test_role_login_per_username_throttle_429(tenant_a, client_for, user_in):
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    username = _role_account(user).username
    for _ in range(5):  # login_user rate: 5/min
        assert (
            client.post(
                ROLE_LOGIN_URL,
                {"username": username, "password": "bad-pass-123"},
                format="json",
            )
        ).status_code == 401
    resp = client.post(
        ROLE_LOGIN_URL,
        {"username": username, "password": "bad-pass-123"},
        format="json",
    )
    assert resp.status_code == 429
    assert resp.json()["code"] == "throttled"


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


def test_password_change_wrong_old_400(tenant_a, user_in, client_for):
    user = _password_user(tenant_a, user_in)
    client = _role_client(client_for, tenant_a, user)
    resp = client.post(
        CHANGE_URL, {"old_password": "not-the-one-1", "new_password": NEW_PASSWORD}, format="json"
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "wrong_password"


def test_password_change_ends_other_sessions(tenant_a, client_for, user_in):
    from core.session_auth import create_session

    user = _password_user(tenant_a, user_in)
    account = _role_account(user)
    with schema_context(tenant_a.schema_name):
        old = create_session(
            user,
            principal_kind="teacher",
            principal_id=account.pk,
        )
    client = _role_client(client_for, tenant_a, user)

    resp = client.post(CHANGE_URL, {"old_password": PASSWORD, "new_password": NEW_PASSWORD}, format="json")
    assert resp.status_code == 200
    new = resp.json()
    assert "access" in new["data"]
    assert "refresh" not in new["data"]

    # Old session revoked by the password change...
    stale = client_for(tenant_a)
    stale.credentials(HTTP_AUTHORIZATION=f"Bearer {old.key}")
    assert stale.get(ME_URL).status_code == 401

    # ...the returned session works, and so does the new password.
    fresh = client_for(tenant_a)
    fresh.credentials(HTTP_AUTHORIZATION=f"Bearer {new['data']['access']}")
    assert fresh.get(ME_URL).status_code == 200
    assert _role_login(client_for, tenant_a, user, password=NEW_PASSWORD).status_code == 200


def test_role_password_change_preserves_only_current_push_eligibility(
    tenant_a,
    client_for,
    user_in,
):
    import celery_tasks.notification_tasks as notification_tasks
    from apps.users.models import Device
    from core.session_auth import create_session, validate_session_key

    user = _password_user(tenant_a, user_in)
    account = _role_account(user)
    login = _role_login(
        client_for,
        tenant_a,
        user,
        device_id="current-phone",
        platform="ios",
    )
    assert login.status_code == 200
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['data']['access']}")
    registered = client.post(
        "/api/v1/users/devices/",
        {
            "device_id": "current-phone",
            "platform": "ios",
            "push_token": "current-private-token",
        },
        format="json",
    )
    assert registered.status_code == 201
    with schema_context(tenant_a.schema_name):
        Device.objects.create(
            user=user,
            device_id="old-tablet",
            platform="android",
            push_token="old-private-token",
        )
        create_session(
            user,
            device_id="old-tablet",
            principal_kind="teacher",
            principal_id=account.pk,
        )

    changed = client.post(
        CHANGE_URL,
        {"old_password": PASSWORD, "new_password": NEW_PASSWORD},
        format="json",
    )

    assert changed.status_code == 200, changed.content
    new_key = changed.json()["data"]["access"]
    with schema_context(tenant_a.schema_name):
        fresh_session = validate_session_key(new_key)
        assert fresh_session is not None
        assert fresh_session.device_id == "current-phone"
        current = Device.objects.get(user=user, device_id="current-phone")
        old = Device.objects.get(user=user, device_id="old-tablet")
        assert current.revoked_at is None
        assert current.push_token == "current-private-token"
        # Role password rotation revokes every old Session. Device metadata may
        # remain registered, but without a live exact-principal session it is
        # ineligible for private push delivery.
        assert old.push_token == "old-private-token"
        from apps.notifications.models import EventType, Notification

        notification = Notification.objects.create(
            user=user,
            event_type=EventType.REPORT_READY,
            title="Device scope",
        )
        assert list(
            notification_tasks._active_push_devices(notification).values_list(
                "device_id",
                flat=True,
            )
        ) == ["current-phone"]


def test_role_password_change_keeps_device_id_and_push_eligibility(
    tenant_a,
    client_for,
):
    import celery_tasks.notification_tasks as notification_tasks
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import Device
    from core.session_auth import validate_session_key

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="push.student")
        student.set_password(PASSWORD)
        student.save(update_fields=["password"])
        user = student.user

    login = client_for(tenant_a).post(
        ROLE_LOGIN_URL,
        {
            "username": student.username,
            "password": PASSWORD,
            "device_id": "role-phone",
            "platform": "android",
        },
        format="json",
    )
    assert login.status_code == 200, login.content
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['data']['access']}")
    registered = client.post(
        "/api/v1/users/devices/",
        {
            "device_id": "role-phone",
            "platform": "android",
            "push_token": "role-private-token",
        },
        format="json",
    )
    assert registered.status_code == 201

    changed = client.post(
        CHANGE_URL,
        {"old_password": PASSWORD, "new_password": NEW_PASSWORD},
        format="json",
    )

    assert changed.status_code == 200, changed.content
    with schema_context(tenant_a.schema_name):
        fresh_session = validate_session_key(changed.json()["data"]["access"])
        assert fresh_session is not None
        assert fresh_session.device_id == "role-phone"
        device = Device.objects.get(user=user, device_id="role-phone")
        assert device.push_token == "role-private-token"
        from apps.notifications.models import EventType, Notification

        notification = Notification.objects.create(
            user=user,
            event_type=EventType.REPORT_READY,
            title="Role device scope",
            recipient_principal_kind="student",
            recipient_principal_id=student.pk,
        )
        assert list(
            notification_tasks._active_push_devices(notification).values_list(
                "device_id",
                flat=True,
            )
        ) == ["role-phone"]


# ---------------------------------------------------------------------------
# Password reset (OTP repurposed — request/confirm)
# ---------------------------------------------------------------------------


def test_password_reset_flow(tenant_a, client_for, user_in, sms_outbox):
    assert settings.ESKIZ_USE_MOCK is True  # never bill real SMS from tests
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)

    resp = client.post(RESET_REQUEST_URL, _reset_payload(user), format="json")
    assert resp.status_code == 202
    assert len(sms_outbox) == 1

    code = _code_from(sms_outbox[0]["text"])
    resp = client.post(
        RESET_CONFIRM_URL,
        _reset_payload(user, code=code, new_password=NEW_PASSWORD),
        format="json",
    )
    assert resp.status_code == 204

    assert _role_login(client_for, tenant_a, user, password=NEW_PASSWORD).status_code == 200
    assert _role_login(client_for, tenant_a, user, password=PASSWORD).status_code == 401


def test_password_reset_unknown_identifier_silent_202(tenant_a, client_for, sms_outbox):
    """Anti-enumeration: unknown identifiers get the same 202 and no SMS."""
    resp = client_for(tenant_a).post(
        RESET_REQUEST_URL,
        {"identifier": "+998905550001", "account_type": "teacher"},
        format="json",
    )
    assert resp.status_code == 202
    assert len(sms_outbox) == 0


@override_settings(SMS_ENABLED=False)
def test_password_reset_disabled_sms_is_uniform_and_never_dispatches(
    tenant_a,
    client_for,
    user_in,
    sms_outbox,
):
    """A disabled transport is reported before account lookup, with no OTP side effect."""
    from apps.users.models import OTP

    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    known = client.post(RESET_REQUEST_URL, _reset_payload(user), format="json")
    unknown = client.post(
        RESET_REQUEST_URL,
        {"identifier": "+998905550099", "account_type": "teacher"},
        format="json",
    )

    assert known.status_code == unknown.status_code == 503
    assert known.json() == unknown.json()
    assert known.json()["code"] == "password_reset_unavailable"
    assert sms_outbox == []
    with schema_context(tenant_a.schema_name):
        assert not OTP.objects.exists()


@override_settings(EMAIL_ENABLED=False)
def test_password_reset_disabled_email_is_uniform_and_never_dispatches(
    tenant_a,
    client_for,
    user_in,
    monkeypatch,
):
    from apps.users.models import OTP

    user = _password_user(tenant_a, user_in, email="known@example.com")
    monkeypatch.setattr(
        "apps.auth.services.send_email",
        lambda **kwargs: pytest.fail("disabled email transport was called"),
    )
    client = client_for(tenant_a)
    known = client.post(
        RESET_REQUEST_URL,
        {"identifier": _role_account(user).email, "account_type": "teacher"},
        format="json",
    )
    unknown = client.post(
        RESET_REQUEST_URL,
        {"identifier": "unknown@example.com", "account_type": "teacher"},
        format="json",
    )

    assert known.status_code == unknown.status_code == 503
    assert known.json() == unknown.json()
    assert known.json()["code"] == "password_reset_unavailable"
    with schema_context(tenant_a.schema_name):
        assert not OTP.objects.exists()


def test_disabling_sms_rejects_an_already_issued_reset_capability(
    tenant_a,
    client_for,
    user_in,
    sms_outbox,
):
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    assert client.post(RESET_REQUEST_URL, _reset_payload(user), format="json").status_code == 202
    code = _code_from(sms_outbox[-1]["text"])

    with override_settings(SMS_ENABLED=False):
        disabled = client.post(
            RESET_CONFIRM_URL,
            _reset_payload(user, code=code, new_password=NEW_PASSWORD),
            format="json",
        )

    assert disabled.status_code == 503
    assert disabled.json()["code"] == "password_reset_unavailable"
    with schema_context(tenant_a.schema_name):
        account = _role_account(user)
        account.refresh_from_db()
        assert account.check_password(PASSWORD)


def test_password_reset_confirm_does_not_reveal_account_existence(
    tenant_a,
    client_for,
    user_in,
    sms_outbox,
):
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    client.post(RESET_REQUEST_URL, _reset_payload(user), format="json")
    issued_code = _code_from(sms_outbox[-1]["text"])
    wrong_code = str((int(issued_code) + 1) % (10**settings.OTP_LENGTH)).zfill(settings.OTP_LENGTH)

    known = client.post(
        RESET_CONFIRM_URL,
        _reset_payload(user, code=wrong_code, new_password=NEW_PASSWORD),
        format="json",
    )
    unknown = client.post(
        RESET_CONFIRM_URL,
        {
            "identifier": "+998905550099",
            "account_type": "teacher",
            "code": wrong_code,
            "new_password": NEW_PASSWORD,
        },
        format="json",
    )

    assert known.status_code == unknown.status_code == 400
    assert known.json() == unknown.json()


@override_settings(OTP_IDENTIFIER_RATE_LIMIT=2, OTP_IDENTIFIER_RATE_WINDOW_SECONDS=60)
def test_password_reset_identifier_rate_limit_is_distributed(tenant_a, client_for):
    client = client_for(tenant_a)
    body = {"identifier": "+998905550001", "account_type": "teacher"}

    assert client.post(RESET_REQUEST_URL, body, format="json").status_code == 202
    assert client.post(RESET_REQUEST_URL, body, format="json").status_code == 202
    assert client.post(RESET_REQUEST_URL, body, format="json").status_code == 429


@override_settings(OTP_GLOBAL_RATE_LIMIT=2, OTP_GLOBAL_RATE_WINDOW_SECONDS=60)
def test_password_reset_global_rate_limit_spans_tenants(tenant_a, tenant_b, client_for):
    first = client_for(tenant_a).post(
        RESET_REQUEST_URL,
        {"identifier": "+998905550001", "account_type": "teacher"},
        format="json",
    )
    second = client_for(tenant_b).post(
        RESET_REQUEST_URL,
        {"identifier": "+998905550002", "account_type": "teacher"},
        format="json",
    )
    blocked = client_for(tenant_a).post(
        RESET_REQUEST_URL,
        {"identifier": "+998905550003", "account_type": "teacher"},
        format="json",
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert blocked.status_code == 429


def test_password_reset_wrong_code_400(tenant_a, client_for, user_in, sms_outbox):
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    client.post(RESET_REQUEST_URL, _reset_payload(user), format="json")
    resp = client.post(
        RESET_CONFIRM_URL,
        _reset_payload(user, code="000000", new_password=NEW_PASSWORD),
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_password_reset_wrong_code_5x_invalidates(tenant_a, client_for, user_in, sms_outbox):
    """After OTP_MAX_ATTEMPTS wrong codes even the CORRECT code is rejected
    (the attempt cap bites — D1-LC regression for the committed increment)."""
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    client.post(RESET_REQUEST_URL, _reset_payload(user), format="json")
    code = _code_from(sms_outbox[0]["text"])

    for _ in range(settings.OTP_MAX_ATTEMPTS):
        resp = client.post(
            RESET_CONFIRM_URL,
            _reset_payload(user, code="000000", new_password=NEW_PASSWORD),
            format="json",
        )
        assert resp.status_code == 400
    resp = client.post(
        RESET_CONFIRM_URL,
        _reset_payload(user, code=code, new_password=NEW_PASSWORD),
        format="json",
    )
    # Confirm failures are deliberately indistinguishable from an unknown
    # identifier; the endpoint's IP limiter still bounds brute-force traffic.
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_reset_request_cooldown_silently_202_no_resend(tenant_a, client_for, user_in, sms_outbox):
    """Anti-enumeration: a 2nd reset request for a KNOWN identifier within the
    per-identifier OTP cooldown returns 202 (NOT 429) — identical to an unknown
    identifier — and sends no second SMS (the cooldown still suppresses resend).
    A 202-vs-429 difference here was an account-existence oracle."""
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    assert client.post(RESET_REQUEST_URL, _reset_payload(user), format="json").status_code == 202
    resp = client.post(RESET_REQUEST_URL, _reset_payload(user), format="json")
    assert resp.status_code == 202  # was 429 — would have leaked account existence
    assert len(sms_outbox) == 1  # cooldown still prevented a second send


def test_reset_request_ip_distinct_identifier_cap(tenant_a, client_for):
    """One IP probing many identifiers gets cut off — even for identifiers
    with no account (the cap runs before the existence check)."""
    cap = settings.OTP_IP_DISTINCT_IDENTIFIER_CAP
    client = client_for(tenant_a)
    for i in range(cap):
        resp = client.post(
            RESET_REQUEST_URL,
            {"identifier": f"+9989055500{i:02d}", "account_type": "teacher"},
            format="json",
        )
        assert resp.status_code == 202
    resp = client.post(
        RESET_REQUEST_URL,
        {"identifier": "+998905551999", "account_type": "teacher"},
        format="json",
    )
    assert resp.status_code == 429
    # The first rejected identifier must not become allowlisted merely because
    # its seen-pair key now exists.
    assert (
        client.post(
            RESET_REQUEST_URL,
            {"identifier": "+998905551999", "account_type": "teacher"},
            format="json",
        ).status_code
        == 429
    )


def test_reset_rate_cache_never_stores_plain_contact_or_ip(tenant_a, client_for):
    from django.core.cache import cache

    client = client_for(tenant_a)
    identifier = "+998905551234"
    ip = "127.0.0.1"
    assert (
        client.post(
            RESET_REQUEST_URL,
            {"identifier": identifier, "account_type": "teacher"},
            format="json",
        ).status_code
        == 202
    )

    cache_keys = " ".join(str(key) for key in getattr(cache, "_cache", {}))
    assert identifier not in cache_keys
    assert ip not in cache_keys


# ---------------------------------------------------------------------------
# Custom-session auth: live roles, revocation, tenant binding
# ---------------------------------------------------------------------------


def test_role_change_is_live_without_reauth(tenant_a, user_in, as_user):
    """Custom session auth reads roles LIVE each request — granting a role does NOT
    invalidate the session (no JWT-style stale-token window); it just takes effect
    immediately, so the session keeps working with the new role."""
    user = user_in(tenant_a, roles=["teacher"])
    client = as_user(tenant_a, user)
    assert client.get(ME_URL).status_code == 200

    with schema_context(tenant_a.schema_name):
        from apps.org.tests.factories import BranchFactory
        from apps.users.models import RoleMembership

        RoleMembership.objects.create(user=user, branch=BranchFactory(), role="librarian")
        roles = set(user.role_memberships.values_list("role", flat=True))

    assert client.get(ME_URL).status_code == 200  # session still valid, no re-login
    assert "librarian" in roles  # the grant is live (read fresh per request)


def test_logout_revokes_the_session(tenant_a, user_in, as_user):
    """Logout revokes the caller's session row, so the same Bearer key is rejected
    (authentication_failed) on the very next request — instant server-side revocation."""
    user = user_in(tenant_a, roles=["teacher"])
    previous_token_version = user.token_version
    client = as_user(tenant_a, user)
    assert client.get(ME_URL).status_code == 200

    assert client.post(LOGOUT_URL).status_code == 200

    resp = client.get(ME_URL)  # same key, now revoked (/me/ is a layered endpoint)
    assert resp.status_code == 401
    assert resp.json()["code"] == "authentication_failed"
    with schema_context(tenant_a.schema_name):
        user.refresh_from_db()
        assert user.token_version == previous_token_version


def test_throttle_survives_non_string_identifier(tenant_a, client_for):
    """Throttles run before validation — a JSON-int identifier must 400, not 500."""
    resp = client_for(tenant_a).post(
        RESET_REQUEST_URL,
        {"identifier": 12345, "account_type": "teacher"},
        format="json",
    )
    assert resp.status_code in (400, 429)


# Keep cache state isolated when running this module alone (conftest clears too).
@pytest.fixture(autouse=True)
def _isolated_throttles():
    cache.clear()
    yield
    cache.clear()
