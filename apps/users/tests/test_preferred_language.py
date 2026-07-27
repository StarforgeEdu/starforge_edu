"""PATCH /api/v1/users/me/ {preferred_language} self-service (D4-LF-3).

Lane F verifies the profile write path that drives the localized notification
template variant. The field already exists (Day-1) and is writable on
UserSerializer; this proves the endpoint round-trips it and stays self-scoped.
"""

from __future__ import annotations

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


def test_patch_me_ignores_read_only_fields(tenant_a, user_in, as_user):
    """username / is_staff are read-only — a PATCH attempting them is a no-op on them."""
    user = user_in(tenant_a, preferred_language="uz")
    original_username = user.username
    client = as_user(tenant_a, user)

    resp = client.patch(
        "/api/v1/users/me/",
        {"preferred_language": "en", "username": "hacker", "is_staff": True},
        format="json",
    )

    assert resp.status_code == 200, resp.content
    user.refresh_from_db()
    assert user.preferred_language == "en"
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
