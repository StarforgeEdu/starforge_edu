from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.auth.services import logout_everywhere
from apps.users.models import Device, Session
from apps.users.services import register_device
from core.session_auth import create_session

pytestmark = pytest.mark.django_db


def test_push_token_moves_off_previous_account_on_shared_device(tenant_a, user_in):
    previous = user_in(tenant_a)
    current = user_in(tenant_a)

    with schema_context(tenant_a.schema_name):
        old_device = register_device(
            user=previous,
            device_id="shared-phone-old-session",
            platform="android",
            push_token="same-fcm-token",
        )
        new_device = register_device(
            user=current,
            device_id="shared-phone-new-session",
            platform="android",
            push_token="same-fcm-token",
        )

        assert old_device is not None
        assert new_device is not None
        old_device.refresh_from_db()
        new_device.refresh_from_db()
        assert old_device.push_token == ""
        assert new_device.push_token == "same-fcm-token"


def test_device_delete_revokes_and_erases_push_token(tenant_a, user_in, as_user):
    user = user_in(tenant_a)
    client = as_user(tenant_a, user)
    response = client.post(
        "/api/v1/users/devices/",
        {
            "device_id": "teacher-phone",
            "platform": "android",
            "push_token": "private-provider-token",
        },
        format="json",
    )
    assert response.status_code == 201
    device_id = response.json()["data"]["id"]

    deleted = client.delete(f"/api/v1/users/devices/{device_id}/")

    assert deleted.status_code == 204
    with schema_context(tenant_a.schema_name):
        device = Device.objects.get(pk=device_id)
        assert device.revoked_at is not None
        assert device.push_token == ""


def test_cannot_revoke_another_users_device(tenant_a, user_in, as_user):
    owner = user_in(tenant_a)
    attacker = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        device = Device.objects.create(
            user=owner,
            device_id="owners-phone",
            platform="ios",
            push_token="owners-token",
        )

    response = as_user(tenant_a, attacker).delete(f"/api/v1/users/devices/{device.pk}/")

    assert response.status_code == 404
    with schema_context(tenant_a.schema_name):
        device.refresh_from_db()
        assert device.revoked_at is None
        assert device.push_token == "owners-token"


def test_logout_everywhere_revokes_sessions_and_erases_all_push_tokens(
    tenant_a,
    user_in,
):
    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        for suffix, platform in (("phone", "ios"), ("tablet", "android")):
            Device.objects.create(
                user=user,
                device_id=suffix,
                platform=platform,
                push_token=f"private-{suffix}-token",
            )
            create_session(user, device_id=suffix)

        logout_everywhere(user)

        assert not Session.objects.filter(user=user, revoked_at__isnull=True).exists()
        devices = list(Device.objects.filter(user=user).order_by("device_id"))
        assert len(devices) == 2
        assert all(device.revoked_at is not None for device in devices)
        assert all(device.push_token == "" for device in devices)


def test_database_rejects_duplicate_nonempty_push_tokens(tenant_a, user_in):
    first = user_in(tenant_a)
    second = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        Device.objects.create(
            user=first,
            device_id="first-phone",
            platform="ios",
            push_token="database-unique-token",
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            Device.objects.create(
                user=second,
                device_id="second-phone",
                platform="android",
                push_token="database-unique-token",
            )


def test_device_registration_rejects_oversized_push_token(
    tenant_a,
    user_in,
    as_user,
):
    user = user_in(tenant_a)
    response = as_user(tenant_a, user).post(
        "/api/v1/users/devices/",
        {
            "device_id": "oversized-token-phone",
            "platform": "android",
            "push_token": "x" * (8 * 1024 + 1),
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    with schema_context(tenant_a.schema_name):
        assert not Device.objects.filter(
            user=user,
            device_id="oversized-token-phone",
        ).exists()
