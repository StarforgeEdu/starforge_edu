"""PATCH /api/v1/users/me/ {preferred_language} self-service (D4-LF-3).

Lane F verifies the profile write path that drives the localized notification
template variant. The field already exists (Day-1) and is writable on
UserSerializer; this proves the endpoint round-trips it and stays self-scoped.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.django_db


def test_patch_me_updates_preferred_language(tenant_a, user_in, as_user):
    user = user_in(tenant_a, preferred_language="uz")
    client = as_user(tenant_a, user)

    resp = client.patch("/api/v1/users/me/", {"preferred_language": "ru"}, format="json")

    assert resp.status_code == 200, resp.content
    assert resp.json()["data"]["preferred_language"] == "ru"
    user.refresh_from_db()
    assert user.preferred_language == "ru"


def test_patch_me_rejects_invalid_language(tenant_a, user_in, as_user):
    user = user_in(tenant_a, preferred_language="uz")
    client = as_user(tenant_a, user)

    resp = client.patch("/api/v1/users/me/", {"preferred_language": "xx"}, format="json")

    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_patch_me_rejects_unknown_and_read_only_fields_atomically(tenant_a, user_in, as_user):
    """A typo or read-only field must not yield a misleading successful update."""
    user = user_in(tenant_a, preferred_language="uz")
    original_username = user.username
    client = as_user(tenant_a, user)

    resp = client.patch(
        "/api/v1/users/me/",
        {"preferred_language": "en", "username": "hacker", "is_staff": True},
        format="json",
    )

    assert resp.status_code == 400, resp.content
    assert resp.json()["code"] == "validation_error"
    assert set(resp.json()["errors"]) == {"is_staff", "username"}
    user.refresh_from_db()
    assert user.preferred_language == "uz"
    assert user.username == original_username
    assert user.is_staff is False


def test_patch_me_requires_auth(tenant_a, client_for):
    client = client_for(tenant_a)
    resp = client.patch("/api/v1/users/me/", {"preferred_language": "ru"}, format="json")
    assert resp.status_code == 401


def test_patch_me_trims_name_whitespace(tenant_a, user_in, as_user):
    """DRF CharField.trim_whitespace parity: a padded name is stored trimmed."""
    user = user_in(tenant_a)
    client = as_user(tenant_a, user)
    resp = client.patch("/api/v1/users/me/", {"first_name": "  John  "}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["data"]["first_name"] == "John"
    user.refresh_from_db()
    assert user.first_name == "John"


def test_patch_me_duplicate_phone_is_field_400_not_409(tenant_a, user_in, as_user):
    """A phone already owned by another user -> a field-specific 400 (DRF
    UniqueValidator parity), not the DB IntegrityError -> generic 409."""
    other = user_in(tenant_a, phone="+998900000002")
    me = user_in(tenant_a, phone="+998900000001")
    client = as_user(tenant_a, me)
    resp = client.patch("/api/v1/users/me/", {"phone": other.phone}, format="json")
    assert resp.status_code == 400, resp.content
    assert resp.json()["code"] == "validation_error"
    assert "phone" in resp.json()["errors"]


def test_patch_me_cannot_deactivate_own_account(tenant_a, user_in, as_user):
    user = user_in(tenant_a)
    response = as_user(tenant_a, user).patch(
        "/api/v1/users/me/",
        {"is_active": False},
        format="json",
    )
    assert response.status_code == 400
    assert "is_active" in response.json()["errors"]
    user.refresh_from_db()
    assert user.is_active is True


def test_role_session_round_trips_preferred_language(tenant_a, client_for):
    from django_tenants.utils import schema_context

    from apps.students.tests.factories import StudentProfileFactory
    from core.session_auth import create_session

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="localized.student")
        student.user.preferred_language = "uz"
        student.user.save(update_fields=["preferred_language"])
        access = create_session(
            student.user,
            principal_kind="student",
            principal_id=student.pk,
        ).key

    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    response = client.patch(
        "/api/v1/users/me/",
        {"preferred_language": "ru"},
        format="json",
    )

    assert response.status_code == 200, response.content
    assert response.json()["data"]["preferred_language"] == "ru"
    with schema_context(tenant_a.schema_name):
        student.user.refresh_from_db()
        assert student.user.preferred_language == "ru"


def test_role_preference_update_does_not_rewrite_identity(tenant_a):
    """A preference-only write must not touch role identity or bridge lifecycle."""
    from django_tenants.utils import schema_context

    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.services import update_role_identity

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="preference-only.student")
        with (
            patch.object(student, "save") as role_save,
            patch("apps.users.services.sync_role_user_bridge") as bridge_sync,
        ):
            update_role_identity(student, {}, preferred_language="ru")

        role_save.assert_not_called()
        bridge_sync.assert_not_called()
        student.user.refresh_from_db()
        assert student.user.preferred_language == "ru"


def test_role_identity_update_writes_only_requested_columns(tenant_a):
    """A stale PATCH must not persist unrelated values from its model instance."""
    from django_tenants.utils import schema_context

    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.services import update_role_identity

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="partial-identity.student")
        with (
            patch.object(student, "save", wraps=student.save) as role_save,
            patch("apps.users.services.sync_role_user_bridge"),
        ):
            update_role_identity(student, {"first_name": "Updated"})

        assert role_save.call_args.kwargs == {
            "update_fields": ["first_name", "updated_at"],
        }


def test_role_session_rejects_unknown_profile_fields(tenant_a, client_for):
    from django_tenants.utils import schema_context

    from apps.students.tests.factories import StudentProfileFactory
    from core.session_auth import create_session

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="strict-profile.student")
        access = create_session(
            student.user,
            principal_kind="student",
            principal_id=student.pk,
        ).key

    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    response = client.patch(
        "/api/v1/users/me/",
        {"preferred_language": "en", "is_active": False},
        format="json",
    )

    assert response.status_code == 400, response.content
    assert response.json()["errors"] == {"is_active": ["This field is not supported."]}
    with schema_context(tenant_a.schema_name):
        student.user.refresh_from_db()
        assert student.user.preferred_language == "uz"


def test_role_session_can_clear_nullable_contact_fields(tenant_a, client_for):
    from django_tenants.utils import schema_context

    from apps.students.tests.factories import StudentProfileFactory
    from core.session_auth import create_session

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(
            username="clear-contact.student",
            phone="+998901112233",
            email="clear-contact@example.test",
        )
        access = create_session(
            student.user,
            principal_kind="student",
            principal_id=student.pk,
        ).key

    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    response = client.patch(
        "/api/v1/users/me/",
        {"phone": None, "email": None},
        format="json",
    )

    assert response.status_code == 200, response.content
    assert response.json()["data"]["phone"] == ""
    assert response.json()["data"]["email"] == ""
    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        assert student.phone == ""
        assert student.email == ""
