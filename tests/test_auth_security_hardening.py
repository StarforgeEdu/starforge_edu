"""Launch-blocker regressions for role-native authentication boundaries."""

import re

import pytest
from django.conf import settings
from django.contrib.auth.hashers import is_password_usable
from django.test import RequestFactory
from django_tenants.utils import schema_context

from core.exceptions import PermissionException

pytestmark = pytest.mark.django_db

ROLE_LOGIN_URL = "/api/v1/auth/role-login/"
RESET_REQUEST_URL = "/api/v1/auth/password/reset/request/"
RESET_CONFIRM_URL = "/api/v1/auth/password/reset/confirm/"
PASSWORD = "Quasar-Lantern-42"
NEW_PASSWORD = "Nebula-Compass-77"


def _code_from(sms_text: str) -> str:
    match = re.search(rf"\b(\d{{{settings.OTP_LENGTH}}})\b", sms_text)
    assert match
    return match.group(1)


def test_admin_linked_staff_profile_cannot_role_login_or_request_reset(tenant_a, client_for, sms_outbox):
    from apps.org.models import StaffProfile
    from apps.users.models import OTP, User

    with schema_context(tenant_a.schema_name):
        admin = User.objects.create_superuser(username="platform-admin", password=PASSWORD)
        staff = StaffProfile(
            user=admin,
            username="platform-admin-role",
            phone="+998901110011",
        )
        staff.set_password(PASSWORD)
        staff.save()

    client = client_for(tenant_a)
    login = client.post(
        ROLE_LOGIN_URL,
        {"username": staff.username, "password": PASSWORD},
        format="json",
    )
    assert login.status_code == 401
    assert login.json()["code"] == "invalid_credentials"

    requested = client.post(
        RESET_REQUEST_URL,
        {"identifier": staff.phone, "account_type": "staff"},
        format="json",
    )
    assert requested.status_code == 202
    assert sms_outbox == []
    with schema_context(tenant_a.schema_name):
        assert not OTP.objects.filter(identifier=staff.phone).exists()
        from core.session_auth import create_session, validate_session_key

        legacy_role_session = create_session(
            admin,
            principal_kind="staff",
            principal_id=staff.pk,
        )
        assert validate_session_key(legacy_role_session.key) is None
        # Even a reset capability created before this guard (or by an internal
        # caller) cannot be used against an administrator-linked role profile.
        from apps.auth.services import send_otp

        send_otp(
            identifier=staff.phone,
            purpose=OTP.PURPOSE_RESET,
            target_kind="staff",
            target_id=staff.pk,
        )
    code = _code_from(sms_outbox[0]["text"])
    rejected_reset = client.post(
        RESET_CONFIRM_URL,
        {
            "identifier": staff.phone,
            "account_type": "staff",
            "code": code,
            "new_password": NEW_PASSWORD,
        },
        format="json",
    )
    assert rejected_reset.status_code == 400
    with schema_context(tenant_a.schema_name):
        admin.refresh_from_db()
        staff.refresh_from_db()
        assert admin.check_password(PASSWORD)
        assert staff.check_password(PASSWORD)


def test_role_login_rejects_cross_table_username_collision(tenant_a, client_for):
    from apps.org.models import StaffProfile
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="duplicate-role-login")
        student.set_password(PASSWORD)
        student.save(update_fields=["password"])
        staff = StaffProfile.objects.create(
            user=UserFactory(),
            username="duplicate-role-login",
        )
        staff.set_password(PASSWORD)
        staff.save(update_fields=["password"])

    response = client_for(tenant_a).post(
        ROLE_LOGIN_URL,
        {"username": "duplicate-role-login", "password": PASSWORD},
        format="json",
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_reset_otp_is_bound_to_exact_role_when_contact_is_shared(tenant_a, client_for, sms_outbox):
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    shared_phone = "+998901110012"
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="shared-student", phone=shared_phone)
        teacher = TeacherProfileFactory(username="shared-teacher", phone=shared_phone)
        student.set_password(PASSWORD)
        teacher.set_password(PASSWORD)
        student.save(update_fields=["password"])
        teacher.save(update_fields=["password"])
        student_id = student.pk
        teacher_id = teacher.pk

    client = client_for(tenant_a)
    requested = client.post(
        RESET_REQUEST_URL,
        {"identifier": shared_phone, "account_type": "student"},
        format="json",
    )
    assert requested.status_code == 202
    code = _code_from(sms_outbox[0]["text"])

    redirected = client.post(
        RESET_CONFIRM_URL,
        {
            "identifier": shared_phone,
            "account_type": "teacher",
            "code": code,
            "new_password": NEW_PASSWORD,
        },
        format="json",
    )
    assert redirected.status_code == 400

    accepted = client.post(
        RESET_CONFIRM_URL,
        {
            "identifier": shared_phone,
            "account_type": "student",
            "code": code,
            "new_password": NEW_PASSWORD,
        },
        format="json",
    )
    assert accepted.status_code == 204
    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        teacher.refresh_from_db()
        assert student.pk == student_id
        assert student.check_password(NEW_PASSWORD)
        assert teacher.pk == teacher_id
        assert teacher.check_password(PASSWORD)


def test_role_session_revalidates_profile_and_bridge_on_every_request(tenant_a):
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory
    from core.session_auth import create_session, validate_session_key

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="live-principal")
        session = create_session(
            student.user,
            principal_kind="student",
            principal_id=student.pk,
        )
        assert validate_session_key(session.key) is not None

        # Simulate an out-of-band profile deactivation that did not touch the bridge
        # or session row. Live principal validation must still reject it immediately.
        StudentProfile.objects.filter(pk=student.pk).update(is_active=False)
        assert validate_session_key(session.key) is None


def test_session_bearer_key_is_hashed_at_rest_and_digest_is_not_a_credential(tenant_a, user_in):
    from apps.users.models import Session
    from core.session_auth import (
        create_session,
        hash_session_key,
        revoke_session,
        validate_session_key,
    )

    user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        issued = create_session(user)
        raw_key = issued.key
        stored = Session.objects.get(pk=issued.pk)

        assert raw_key
        assert stored.key == ""  # raw credentials are issuance-only
        assert stored.key_hash == hash_session_key(raw_key)
        assert raw_key not in stored.key_hash
        assert validate_session_key(raw_key).pk == issued.pk
        # A database-only leak must not turn the stored digest into a Bearer token.
        assert validate_session_key(stored.key_hash) is None

        revoke_session(raw_key)
        assert validate_session_key(raw_key) is None


def test_session_idle_timeout_is_enforced_and_revokes_the_row(tenant_a, user_in):
    from datetime import timedelta

    from django.test import override_settings
    from django.utils import timezone

    from apps.users.models import Session
    from core.session_auth import create_session, validate_session_key

    user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name), override_settings(SESSION_IDLE_TIMEOUT_MINUTES=30):
        issued = create_session(user)
        Session.objects.filter(pk=issued.pk).update(last_used_at=timezone.now() - timedelta(minutes=31))

        assert validate_session_key(issued.key) is None
        issued.refresh_from_db()
        assert issued.revoked_at is not None


def test_passive_websocket_validation_does_not_extend_idle_activity(tenant_a, user_in):
    from datetime import timedelta

    from django.utils import timezone

    from apps.users.models import Session
    from core.session_auth import create_session, validate_session_id

    user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        issued = create_session(user)
        stale_activity = timezone.now() - timedelta(minutes=2)
        Session.objects.filter(pk=issued.pk).update(last_used_at=stale_activity)

        assert validate_session_id(issued.pk, expected_user_id=user.pk, touch=False) is not None
        issued.refresh_from_db()
        assert issued.last_used_at == stale_activity


def test_legacy_plaintext_session_is_dual_read_then_lazily_hashed(tenant_a, user_in):
    import secrets
    from datetime import timedelta

    from django.utils import timezone

    from apps.users.models import Session
    from core.session_auth import hash_session_key, validate_session_key

    user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        legacy_key = secrets.token_urlsafe(40)
        legacy = Session.objects.create(
            user=user,
            key_hash=legacy_key,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        assert validate_session_key(legacy_key).pk == legacy.pk
        legacy.refresh_from_db()
        assert legacy.key_hash == hash_session_key(legacy_key)


def test_hash_session_keys_command_is_batched_idempotent_and_supports_dry_run(tenant_a, user_in):
    import secrets
    from datetime import timedelta
    from io import StringIO

    from django.core.management import call_command
    from django.utils import timezone

    from apps.users.models import Session
    from core.session_auth import create_session, hash_session_key

    user = user_in(tenant_a, roles=["teacher"])
    raw_keys = [secrets.token_urlsafe(40), secrets.token_urlsafe(40)]
    with schema_context(tenant_a.schema_name):
        legacy_ids = [
            Session.objects.create(
                user=user,
                key_hash=raw_key,
                expires_at=timezone.now() + timedelta(hours=1),
            ).pk
            for raw_key in raw_keys
        ]
        already_hashed = create_session(user)
        original_hash = already_hashed.key_hash

    dry_output = StringIO()
    call_command(
        "hash_session_keys",
        schema_names=[tenant_a.schema_name],
        batch_size=1,
        dry_run=True,
        stdout=dry_output,
    )
    assert not any(raw_key in dry_output.getvalue() for raw_key in raw_keys)
    with schema_context(tenant_a.schema_name):
        assert (
            list(Session.objects.filter(pk__in=legacy_ids).order_by("pk").values_list("key_hash", flat=True))
            == raw_keys
        )

    output = StringIO()
    call_command(
        "hash_session_keys",
        schema_names=[tenant_a.schema_name],
        batch_size=1,
        stdout=output,
    )
    call_command(
        "hash_session_keys",
        schema_names=[tenant_a.schema_name],
        batch_size=1,
        stdout=output,
    )
    with schema_context(tenant_a.schema_name):
        stored = list(
            Session.objects.filter(pk__in=legacy_ids).order_by("pk").values_list("key_hash", flat=True)
        )
        assert stored == [hash_session_key(raw_key) for raw_key in raw_keys]
        already_hashed.refresh_from_db()
        assert already_hashed.key_hash == original_hash


def test_session_hash_bulk_cutover_is_post_readiness_not_a_schema_migration():
    import importlib
    from pathlib import Path

    from django.db import migrations

    migration = importlib.import_module("apps.users.migrations.0007_hash_session_keys_at_rest")
    assert not any(
        isinstance(operation, migrations.RunPython) for operation in migration.Migration.operations
    )

    root = Path(__file__).resolve().parent.parent
    deploy_script = (root / "scripts" / "deploy_production.sh").read_text(encoding="utf-8")
    readiness_gate = deploy_script.index('if [[ "$healthy" != "1" ]]')
    cutover = deploy_script.index("python manage.py hash_session_keys")
    release_marker = deploy_script.index('atomic_marker "${DEPLOY_DIR}/current_release"')
    assert readiness_gate < cutover < release_marker


def test_profile_delete_disables_bridge_grants_devices_and_sessions(tenant_a):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import Device, RoleMembership, Session, User
    from core.session_auth import create_session

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="delete-principal")
        user_id = student.user_id
        membership = RoleMembership.objects.create(
            user_id=user_id,
            branch=BranchFactory(),
            role="student",
        )
        device = Device.objects.create(user_id=user_id, device_id="phone-1", platform="android")
        session = create_session(
            student.user,
            principal_kind="student",
            principal_id=student.pk,
        )

        student.delete()  # exercises the shared pre-delete safety net

        bridge = User.objects.get(pk=user_id)
        membership.refresh_from_db()
        device.refresh_from_db()
        stored_session = Session.objects.get(pk=session.pk)
        assert bridge.is_active is False
        assert not is_password_usable(bridge.password)
        assert membership.revoked_at is not None
        assert device.revoked_at is not None
        assert stored_session.revoked_at is not None


def test_profile_deactivation_revokes_bridge_grants_and_sessions(tenant_a):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership, Session
    from apps.users.services import update_role_identity
    from core.session_auth import create_session

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="deactivate-principal")
        membership = RoleMembership.objects.create(
            user=student.user,
            branch=BranchFactory(),
            role="student",
        )
        session = create_session(
            student.user,
            principal_kind="student",
            principal_id=student.pk,
        )

        update_role_identity(student, {"is_active": False})

        student.refresh_from_db()
        student.user.refresh_from_db()
        membership.refresh_from_db()
        stored_session = Session.objects.get(pk=session.pk)
        assert student.is_active is False
        assert student.user.is_active is False
        assert not student.has_usable_password()
        assert not student.user.has_usable_password()
        assert membership.revoked_at is not None
        assert stored_session.revoked_at is not None


def test_read_only_session_centrally_rejects_unsafe_method(tenant_a, user_in):
    from core.session_auth import SessionAuthentication, create_session

    user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        session = create_session(user, read_only=True)
        request = RequestFactory().post("/any-write/")
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {session.key}"
        with pytest.raises(PermissionException) as exc_info:
            SessionAuthentication().authenticate(request)
        assert exc_info.value.code == "read_only_token"
