"""Channels + Celery plumbing (D1-LE-6). Documents the pattern for D4-C."""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.utils import timezone
from django_tenants.utils import schema_context

from config.asgi import application

HOST_HEADERS = [(b"host", b"a.localhost")]
HOST_HEADERS_B = [(b"host", b"b.localhost")]


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ws_anonymous_rejected(tenant_a):
    comm = WebsocketCommunicator(application, "/ws/ping/", headers=HOST_HEADERS)
    connected, close_code = await comm.connect()
    assert not connected
    assert close_code == 4401  # PingConsumer rejects anonymous


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ws_authed_connect_accepted(tenant_a, user_in):
    """D1-LE-6 acceptance: a JWT-authed connect is accepted and greeted.

    Pins the middleware doing the user lookup under the tenant schema on the
    database_sync_to_async worker thread (the loop-thread set_tenant bug)."""
    from apps.auth.services import issue_token

    @sync_to_async
    def _mint():
        user = user_in(tenant_a)  # creates the user inside tenant_a's schema
        with schema_context(tenant_a.schema_name):
            return user.pk, issue_token(user)["access"]

    user_pk, token = await _mint()
    comm = WebsocketCommunicator(application, f"/ws/ping/?token={token}", headers=HOST_HEADERS)
    connected, _ = await comm.connect()
    assert connected
    assert await comm.receive_json_from() == {"type": "hello", "user_id": user_pk}
    await comm.disconnect()


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ws_cross_tenant_token_rejected(tenant_a, tenant_b, user_in):
    """TD-1 over Channels: a tenant_a token presented on tenant_b's host must
    NOT authenticate (schema claim != resolved tenant -> AnonymousUser -> 4401)."""
    from apps.auth.services import issue_token

    @sync_to_async
    def _mint():
        user = user_in(tenant_a)
        with schema_context(tenant_a.schema_name):
            return issue_token(user)["access"]

    token = await _mint()
    comm = WebsocketCommunicator(application, f"/ws/ping/?token={token}", headers=HOST_HEADERS_B)
    connected, close_code = await comm.connect()
    assert not connected
    assert close_code == 4401


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ws_revoked_session_rejected(tenant_a, user_in):
    """Custom session auth over Channels: revoking the session (logout / password
    change) rejects the websocket key too — the consumer closes 4401."""
    from apps.auth.services import issue_token
    from core.session_auth import revoke_all_for_user

    @sync_to_async
    def _mint_and_revoke():
        user = user_in(tenant_a)
        with schema_context(tenant_a.schema_name):
            token = issue_token(user)["access"]
            revoke_all_for_user(user.pk)
        return token

    token = await _mint_and_revoke()
    comm = WebsocketCommunicator(application, f"/ws/ping/?token={token}", headers=HOST_HEADERS)
    connected, close_code = await comm.connect()
    assert not connected
    assert close_code == 4401


@pytest.mark.django_db
def test_celery_eager_purges_expired_otp(tenant_a):
    from apps.users.models import OTP
    from celery_tasks.cleanup_tasks import purge_expired_otps

    with schema_context(tenant_a.schema_name):
        OTP.objects.create(
            identifier="+998900000999",
            channel=OTP.CHANNEL_SMS,
            code_hash="x",
            expires_at=timezone.now() - timedelta(hours=1),
        )
        purge_expired_otps()  # runs synchronously under CELERY_TASK_ALWAYS_EAGER
        assert OTP.objects.filter(expires_at__lt=timezone.now()).count() == 0
