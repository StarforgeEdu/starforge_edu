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

LOGIN_URL = "/api/v1/auth/login/"
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


def _password_user(tenant, user_in, *, roles=("teacher",), password=PASSWORD):
    user = user_in(tenant, roles=list(roles))
    with schema_context(tenant.schema_name):
        user.set_password(password)
        user.save(update_fields=["password"])
    return user


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_happy_path_registers_device(tenant_a, client_for, user_in):
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)

    resp = client.post(
        LOGIN_URL,
        {"username": user.username, "password": PASSWORD, "device_id": "dev-1", "platform": "android"},
        format="json",
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


def test_role_login_student_signs_in_as_their_role(tenant_a, client_for):
    """Role-native login: a student authenticates with their student account's username and
    the (linked-user) password. The session binds to the linked User so /me works exactly as
    before, and the response reports which role they logged in as."""
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
    assert me.json()["data"]["account_type"] == "student"


def test_role_login_wrong_password_401(tenant_a, client_for):
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="bob.student")
        student.user.set_password(PASSWORD)
        student.user.save(update_fields=["password"])

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
    """Regression (auth review): role-login checks the LINKED USER's password (single source
    of truth), so a password change/reset takes effect immediately — the OLD password 401s and
    the NEW one works — with no drift from a stale role-account snapshot."""
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


def test_login_wrong_password_401(tenant_a, client_for, user_in):
    user = _password_user(tenant_a, user_in)
    resp = client_for(tenant_a).post(
        LOGIN_URL, {"username": user.username, "password": "wrong-wrong-1"}, format="json"
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


def test_login_unknown_username_same_error(tenant_a, client_for):
    resp = client_for(tenant_a).post(
        LOGIN_URL, {"username": "ghost-user", "password": "whatever-123"}, format="json"
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


def test_login_inactive_user_same_error(tenant_a, client_for, user_in):
    user = _password_user(tenant_a, user_in)
    with schema_context(tenant_a.schema_name):
        user.is_active = False
        user.save(update_fields=["is_active"])
    resp = client_for(tenant_a).post(
        LOGIN_URL, {"username": user.username, "password": PASSWORD}, format="json"
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


def test_login_per_username_throttle_429(tenant_a, client_for, user_in):
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    for _ in range(5):  # login_user rate: 5/min
        assert (
            client.post(LOGIN_URL, {"username": user.username, "password": "bad-pass-123"}, format="json")
        ).status_code == 401
    resp = client.post(LOGIN_URL, {"username": user.username, "password": "bad-pass-123"}, format="json")
    assert resp.status_code == 429
    assert resp.json()["code"] == "throttled"


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


def test_password_change_wrong_old_400(tenant_a, user_in, as_user):
    user = _password_user(tenant_a, user_in)
    client = as_user(tenant_a, user)
    resp = client.post(
        CHANGE_URL, {"old_password": "not-the-one-1", "new_password": NEW_PASSWORD}, format="json"
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "wrong_password"


def test_password_change_ends_other_sessions(tenant_a, client_for, user_in, as_user):
    from apps.auth.services import issue_token

    user = _password_user(tenant_a, user_in)
    with schema_context(tenant_a.schema_name):
        old = issue_token(user)
    client = as_user(tenant_a, user)

    resp = client.post(CHANGE_URL, {"old_password": PASSWORD, "new_password": NEW_PASSWORD}, format="json")
    assert resp.status_code == 200
    new = resp.json()
    assert "access" in new["data"]
    assert "refresh" not in new["data"]

    # Old session revoked by the password change...
    stale = client_for(tenant_a)
    stale.credentials(HTTP_AUTHORIZATION=f"Bearer {old['access']}")
    assert stale.get(ME_URL).status_code == 401

    # ...the returned session works, and so does the new password.
    fresh = client_for(tenant_a)
    fresh.credentials(HTTP_AUTHORIZATION=f"Bearer {new['data']['access']}")
    assert fresh.get(ME_URL).status_code == 200
    assert (
        client_for(tenant_a).post(
            LOGIN_URL, {"username": user.username, "password": NEW_PASSWORD}, format="json"
        )
    ).status_code == 200


# ---------------------------------------------------------------------------
# Password reset (OTP repurposed — request/confirm)
# ---------------------------------------------------------------------------


def test_password_reset_flow(tenant_a, client_for, user_in, sms_outbox):
    assert settings.ESKIZ_USE_MOCK is True  # never bill real SMS from tests
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)

    resp = client.post(RESET_REQUEST_URL, {"identifier": user.phone}, format="json")
    assert resp.status_code == 202
    assert len(sms_outbox) == 1

    code = _code_from(sms_outbox[0]["text"])
    resp = client.post(
        RESET_CONFIRM_URL,
        {"identifier": user.phone, "code": code, "new_password": NEW_PASSWORD},
        format="json",
    )
    assert resp.status_code == 204

    assert (
        client.post(LOGIN_URL, {"username": user.username, "password": NEW_PASSWORD}, format="json")
    ).status_code == 200
    assert (
        client.post(LOGIN_URL, {"username": user.username, "password": PASSWORD}, format="json")
    ).status_code == 401


def test_password_reset_unknown_identifier_silent_202(tenant_a, client_for, sms_outbox):
    """Anti-enumeration: unknown identifiers get the same 202 and no SMS."""
    resp = client_for(tenant_a).post(RESET_REQUEST_URL, {"identifier": "+998905550001"}, format="json")
    assert resp.status_code == 202
    assert len(sms_outbox) == 0


@override_settings(OTP_IDENTIFIER_RATE_LIMIT=2, OTP_IDENTIFIER_RATE_WINDOW_SECONDS=60)
def test_password_reset_identifier_rate_limit_is_distributed(tenant_a, client_for):
    client = client_for(tenant_a)
    body = {"identifier": "+998905550001"}

    assert client.post(RESET_REQUEST_URL, body, format="json").status_code == 202
    assert client.post(RESET_REQUEST_URL, body, format="json").status_code == 202
    assert client.post(RESET_REQUEST_URL, body, format="json").status_code == 429


@override_settings(OTP_GLOBAL_RATE_LIMIT=2, OTP_GLOBAL_RATE_WINDOW_SECONDS=60)
def test_password_reset_global_rate_limit_spans_tenants(tenant_a, tenant_b, client_for):
    first = client_for(tenant_a).post(
        RESET_REQUEST_URL,
        {"identifier": "+998905550001"},
        format="json",
    )
    second = client_for(tenant_b).post(
        RESET_REQUEST_URL,
        {"identifier": "+998905550002"},
        format="json",
    )
    blocked = client_for(tenant_a).post(
        RESET_REQUEST_URL,
        {"identifier": "+998905550003"},
        format="json",
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert blocked.status_code == 429


def test_password_reset_wrong_code_400(tenant_a, client_for, user_in, sms_outbox):
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    client.post(RESET_REQUEST_URL, {"identifier": user.phone}, format="json")
    resp = client.post(
        RESET_CONFIRM_URL,
        {"identifier": user.phone, "code": "000000", "new_password": NEW_PASSWORD},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_password_reset_wrong_code_5x_invalidates(tenant_a, client_for, user_in, sms_outbox):
    """After OTP_MAX_ATTEMPTS wrong codes even the CORRECT code is rejected
    (the attempt cap bites — D1-LC regression for the committed increment)."""
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    client.post(RESET_REQUEST_URL, {"identifier": user.phone}, format="json")
    code = _code_from(sms_outbox[0]["text"])

    for _ in range(settings.OTP_MAX_ATTEMPTS):
        resp = client.post(
            RESET_CONFIRM_URL,
            {"identifier": user.phone, "code": "000000", "new_password": NEW_PASSWORD},
            format="json",
        )
        assert resp.status_code == 400
    resp = client.post(
        RESET_CONFIRM_URL,
        {"identifier": user.phone, "code": code, "new_password": NEW_PASSWORD},
        format="json",
    )
    assert resp.status_code == 429
    assert resp.json()["code"] == "throttled"


def test_reset_request_cooldown_silently_202_no_resend(tenant_a, client_for, user_in, sms_outbox):
    """Anti-enumeration: a 2nd reset request for a KNOWN identifier within the
    per-identifier OTP cooldown returns 202 (NOT 429) — identical to an unknown
    identifier — and sends no second SMS (the cooldown still suppresses resend).
    A 202-vs-429 difference here was an account-existence oracle."""
    user = _password_user(tenant_a, user_in)
    client = client_for(tenant_a)
    assert client.post(RESET_REQUEST_URL, {"identifier": user.phone}, format="json").status_code == 202
    resp = client.post(RESET_REQUEST_URL, {"identifier": user.phone}, format="json")
    assert resp.status_code == 202  # was 429 — would have leaked account existence
    assert len(sms_outbox) == 1  # cooldown still prevented a second send


def test_reset_request_ip_distinct_identifier_cap(tenant_a, client_for):
    """One IP probing many identifiers gets cut off — even for identifiers
    with no account (the cap runs before the existence check)."""
    cap = settings.OTP_IP_DISTINCT_IDENTIFIER_CAP
    client = client_for(tenant_a)
    for i in range(cap):
        resp = client.post(RESET_REQUEST_URL, {"identifier": f"+9989055500{i:02d}"}, format="json")
        assert resp.status_code == 202
    resp = client.post(RESET_REQUEST_URL, {"identifier": "+998905551999"}, format="json")
    assert resp.status_code == 429


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

    assert client.post(LOGOUT_URL).status_code == 204

    resp = client.get(ME_URL)  # same key, now revoked (/me/ is a layered endpoint)
    assert resp.status_code == 401
    assert resp.json()["code"] == "authentication_failed"
    with schema_context(tenant_a.schema_name):
        user.refresh_from_db()
        assert user.token_version == previous_token_version + 1


def test_throttle_survives_non_string_identifier(tenant_a, client_for):
    """Throttles run before validation — a JSON-int identifier must 400, not 500."""
    resp = client_for(tenant_a).post(RESET_REQUEST_URL, {"identifier": 12345}, format="json")
    assert resp.status_code in (400, 429)


# Keep cache state isolated when running this module alone (conftest clears too).
@pytest.fixture(autouse=True)
def _isolated_throttles():
    cache.clear()
    yield
    cache.clear()
