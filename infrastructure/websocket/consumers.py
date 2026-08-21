"""Shared Channels consumer building blocks (D4-LC-2).

``HeartbeatConsumerMixin`` gives every real consumer a server-driven liveness
check: the server sends ``{"type":"ping"}`` every ``HEARTBEAT_INTERVAL`` seconds
and the client must answer ``{"type":"pong"}``. After ``HEARTBEAT_MAX_MISSED``
unanswered pings the socket is closed 4408 (a dead/half-open connection that the
TCP keepalive would otherwise hold open for minutes, leaking a worker slot and a
Redis group membership).

``PingConsumer`` is the unchanged v1 smoke consumer (``/ws/ping/``).

Group cleanup contract: subclasses register every group they join in
``self._groups`` (via ``join_group``); ``disconnect`` cancels the heartbeat task
AND ``group_discard``-s every joined group, so a dropped socket never leaves a
stale membership on the shared channel layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

logger = logging.getLogger(__name__)


@database_sync_to_async
def _session_still_valid(session_id: int, schema: str | None, user_id: int) -> bool:
    """Revalidate the tenant, session, user, and role-native principal live."""

    from core.session_auth import session_requires_password_change, validate_session_id

    if not schema:
        return False
    Tenant = get_tenant_model()
    with schema_context(get_public_schema_name()):
        tenant_is_live = Tenant.objects.filter(
            schema_name=schema,
            is_active=True,
            archived_at__isnull=True,
        ).exists()
    if not tenant_is_live:
        return False
    with schema_context(schema):
        # A passive background socket is not proof of interactive user activity;
        # heartbeat/event checks must enforce, but never extend, the idle window.
        session = validate_session_id(session_id, expected_user_id=user_id, touch=False)
        return session is not None and not session_requires_password_change(session)


# Server ping cadence and tolerance. Class attributes so tests can patch the
# interval down (a 30s real interval would make the heartbeat tests glacial).
HEARTBEAT_INTERVAL = 30  # seconds between server pings
HEARTBEAT_MAX_MISSED = 2  # consecutive unanswered pings before close 4408
# App-level inbound frame cap (in addition to any ASGI server limit): these are
# tiny JSON control frames (pong / small commands), so anything larger is dropped
# undecoded as a cheap DoS guard.
MAX_INBOUND_BYTES = 64 * 1024
MAX_OUTBOUND_BYTES = 64 * 1024
INBOUND_RATE_WINDOW = 10.0
INBOUND_RATE_LIMIT = 30

# Close codes (also documented in agents/API-CONTRACT.md "Realtime").
CLOSE_UNAUTHORIZED = 4401  # anonymous / cross-tenant / stale tv
CLOSE_FORBIDDEN = 4403  # authenticated but not permitted (branch scope)
CLOSE_HEARTBEAT = 4408  # heartbeat timeout (missed pongs)
CLOSE_INVALID_FRAME = 4400  # binary / malformed JSON control frame
CLOSE_TOO_LARGE = 4409  # inbound/outbound application frame exceeds the bound
CLOSE_RATE_LIMITED = 4429  # connection/connection-frame rate limit
CLOSE_INTERNAL = 1011  # unexpected channel/session dependency failure

APPLICATION_SUBPROTOCOL = "starforge.v1"

_LEASE_COMPARE_REFRESH_SQL = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_LEASE_COMPARE_DELETE_SQL = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
_LOCAL_LEASE_LOCK = threading.Lock()


def _redis_connection_lease_client():
    """Return the production Redis client used for atomic lease ownership.

    Unit/development settings may deliberately use LocMemCache; those calls are
    serialized by ``_LOCAL_LEASE_LOCK`` below. Production's configured Django
    Redis backend uses the same ``REDIS_URL`` as the direct client and therefore
    gets cross-process compare-and-expire/delete semantics via Lua.
    """

    backend = str(settings.CACHES.get("default", {}).get("BACKEND", ""))
    if backend != "django.core.cache.backends.redis.RedisCache":
        return None
    from infrastructure.cache.redis_client import get_redis

    return get_redis()


@sync_to_async
def _claim_connection_lease(*, schema: str, session_id: int, channel_name: str) -> str | None:
    limit = int(getattr(settings, "WEBSOCKET_MAX_CONNECTIONS_PER_SESSION", 5))
    timeout = int(getattr(settings, "WEBSOCKET_CONNECTION_LEASE_SECONDS", 90))
    redis = _redis_connection_lease_client()
    for slot in range(limit):
        key = f"ws-connection:{schema}:{session_id}:{slot}"
        claimed = (
            bool(redis.set(key, channel_name, ex=timeout, nx=True))
            if redis is not None
            else cache.add(key, channel_name, timeout=timeout)
        )
        if claimed:
            return key
    return None


@sync_to_async
def _refresh_connection_lease(key: str, channel_name: str) -> bool:
    timeout = int(getattr(settings, "WEBSOCKET_CONNECTION_LEASE_SECONDS", 90))
    redis = _redis_connection_lease_client()
    if redis is not None:
        return bool(
            redis.eval(
                _LEASE_COMPARE_REFRESH_SQL,
                1,
                key,
                channel_name,
                timeout,
            )
        )
    with _LOCAL_LEASE_LOCK:
        if cache.get(key) != channel_name:
            return False
        return bool(cache.touch(key, timeout=timeout))


@sync_to_async
def _release_connection_lease(key: str, channel_name: str) -> None:
    redis = _redis_connection_lease_client()
    if redis is not None:
        redis.eval(_LEASE_COMPARE_DELETE_SQL, 1, key, channel_name)
        return
    with _LOCAL_LEASE_LOCK:
        if cache.get(key) == channel_name:
            cache.delete(key)


def accepted_subprotocol(scope) -> str | None:
    """The subprotocol to echo in the handshake (D4-LC fix).

    RFC 6455 §4.2.2: the server's selected subprotocol MUST be one of the values
    the client offered. Browsers authenticate by offering a SINGLE value
    A bearer credential is transport metadata, not an application protocol.
    Echoing ``bearer.<token>`` copies a live credential into the response header,
    browser ``WebSocket.protocol`` property, and often proxy telemetry. Select
    only the optional safe application protocol; otherwise accept without one.
    """
    for offered in scope.get("subprotocols", []) or []:
        if offered == APPLICATION_SUBPROTOCOL:
            return APPLICATION_SUBPROTOCOL
    return None


class HeartbeatConsumerMixin(AsyncJsonWebsocketConsumer):
    """Adds a server heartbeat + tracked group membership to a JSON consumer.

    Subclasses MUST call ``await self.start_heartbeat()`` after ``accept()`` and
    join groups via ``await self.join_group(name)``. ``receive_json`` here only
    consumes the client ``pong``; subclasses overriding it should ``super()``
    or handle ``{"type":"pong"}`` themselves.
    """

    HEARTBEAT_INTERVAL = HEARTBEAT_INTERVAL
    HEARTBEAT_MAX_MISSED = HEARTBEAT_MAX_MISSED

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._groups: set[str] = set()
        self._heartbeat_task: asyncio.Task | None = None
        self._missed_pings = 0
        self._inbound_times: deque[float] = deque(maxlen=INBOUND_RATE_LIMIT)
        self._connection_lease_key: str | None = None
        self._connection_slot_dependency_failed = False

    async def claim_connection_slot(self) -> bool:
        """Claim one cross-process, expiring slot for this tenant session."""

        schema = self._schema()
        session_id = self.scope.get("_ws_session_id")
        self._connection_slot_dependency_failed = False
        if not schema or not isinstance(session_id, int):
            return False
        try:
            self._connection_lease_key = await _claim_connection_lease(
                schema=schema,
                session_id=session_id,
                channel_name=self.channel_name,
            )
        except Exception:
            logger.warning("WebSocket connection-limit dependency failed.", exc_info=True)
            self._connection_slot_dependency_failed = True
            return False
        return self._connection_lease_key is not None

    def connection_slot_denial_code(self) -> int:
        """Distinguish actual saturation from a fail-closed cache outage."""

        return CLOSE_INTERNAL if self._connection_slot_dependency_failed else CLOSE_RATE_LIMITED

    async def _refresh_connection_slot(self) -> bool:
        key = self._connection_lease_key
        if key is None:
            return False
        try:
            return await _refresh_connection_lease(key, self.channel_name)
        except Exception:
            logger.warning("WebSocket connection lease refresh failed.", exc_info=True)
            return False

    async def _release_connection_slot(self) -> None:
        key = self._connection_lease_key
        self._connection_lease_key = None
        if key is None:
            return
        try:
            await _release_connection_lease(key, self.channel_name)
        except Exception:
            logger.warning("WebSocket connection lease cleanup failed.", exc_info=True)

    # -- group tracking ---------------------------------------------------
    async def join_group(self, group: str) -> None:
        """Add this channel to ``group`` and remember it for cleanup."""
        await self.channel_layer.group_add(group, self.channel_name)
        self._groups.add(group)

    # -- heartbeat --------------------------------------------------------
    async def start_heartbeat(self) -> None:
        self._missed_pings = 0
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                # Re-authorize the LIVE socket each cycle (R1-05): a session revoked/
                # expired after connect (force-logout, password change, deactivation), or a
                # revoked role/branch on a scoped consumer, must terminate the stream — not
                # keep delivering the tenant's realtime feed until the client disconnects.
                if not await self._reauthorize():
                    return
                if not await self._refresh_connection_slot():
                    await self._close_with_cleanup(CLOSE_INTERNAL)
                    return
                # Refresh Redis group TTLs so a short group_expiry can reap
                # memberships left by process crashes without expiring healthy,
                # long-lived sockets.
                for group in tuple(self._groups):
                    await self.channel_layer.group_add(group, self.channel_name)
                # The ping we are about to send counts against the budget until a
                # pong clears it. Two pings sent with no intervening pong = close.
                self._missed_pings += 1
                if self._missed_pings > self.HEARTBEAT_MAX_MISSED:
                    # Server-initiated close does NOT trigger websocket_disconnect,
                    # so discard groups here to avoid a membership leak on 4408.
                    await self._close_with_cleanup(CLOSE_HEARTBEAT)
                    return
                await self.send_json({"type": "ping"})
        except asyncio.CancelledError:  # pragma: no cover - normal on disconnect
            raise
        except Exception:
            # Dependency failures must fail closed and release Redis groups. The
            # raw bearer key is never stored in scope, so traceback context cannot
            # copy a credential into logs.
            logger.warning("WebSocket heartbeat authorization failed.", exc_info=True)
            await self._close_with_cleanup(CLOSE_INTERNAL)

    async def _reauthorize(self) -> bool:
        """Re-check the live socket's authorization. On failure it discards groups, closes
        the socket, and returns False: 4401 when the session is gone (force-logout /
        expiry / deactivation), 4403 when the consumer's own scope check now fails."""
        session_id = self.scope.get("_ws_session_id")
        user = self._authed_user()
        if (
            not isinstance(session_id, int)
            or user is None
            or not await _session_still_valid(session_id, self._schema(), user.pk)
        ):
            await self._close_with_cleanup(CLOSE_UNAUTHORIZED)
            return False
        if not await self._still_authorized():
            await self._close_with_cleanup(CLOSE_FORBIDDEN)
            return False
        return True

    async def _still_authorized(self) -> bool:
        """Overridable per-consumer authorization re-check, run every heartbeat. Defaults to
        no extra check beyond the session (any authenticated user stays). A branch/role-
        scoped consumer (e.g. attendance) overrides this to drop a now-unauthorized socket."""
        return True

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        now = time.monotonic()
        while self._inbound_times and now - self._inbound_times[0] >= INBOUND_RATE_WINDOW:
            self._inbound_times.popleft()
        if len(self._inbound_times) >= INBOUND_RATE_LIMIT:
            await self._close_with_cleanup(CLOSE_RATE_LIMITED)
            return
        self._inbound_times.append(now)
        # Drop an oversized inbound frame undecoded (DoS guard) before the JSON
        # parse; otherwise preserve the base behavior.
        if bytes_data is not None:
            await self._close_invalid_frame()
            return
        if text_data is not None and (
            len(text_data) > MAX_INBOUND_BYTES or len(text_data.encode("utf-8")) > MAX_INBOUND_BYTES
        ):
            await self._close_with_cleanup(CLOSE_TOO_LARGE)
            return
        try:
            await super().receive(text_data=text_data, bytes_data=None, **kwargs)
        except (json.JSONDecodeError, TypeError, ValueError):
            await self._close_invalid_frame()

    async def _close_invalid_frame(self) -> None:
        """Close malformed clients without leaking their tracked Redis groups."""
        await self._close_with_cleanup(CLOSE_INVALID_FRAME)

    async def _close_with_cleanup(self, code: int) -> None:
        if self._heartbeat_task is not None and self._heartbeat_task is not asyncio.current_task():
            self._heartbeat_task.cancel()
        if self._heartbeat_task is not None:
            self._heartbeat_task = None
        await self._discard_groups()
        await self._release_connection_slot()
        await self.close(code=code)

    async def send_bounded_event(self, *, event_type: str, payload: dict) -> bool:
        """Reauthorize immediately before a push and bound its serialized size."""

        try:
            if not await self._reauthorize():
                return False
            envelope = {"type": event_type, "payload": payload}
            encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
        except (TypeError, ValueError):
            logger.warning("Dropped a non-JSON realtime %s event.", event_type)
            return False
        except Exception:
            logger.warning("Realtime event authorization failed.", exc_info=True)
            await self._close_with_cleanup(CLOSE_INTERNAL)
            return False
        if len(encoded) > MAX_OUTBOUND_BYTES:
            logger.warning("Dropped an oversized realtime %s event.", event_type)
            return False
        await self.send_json(envelope)
        return True

    async def receive_json(self, content, **kwargs):
        # A non-dict JSON frame (e.g. "hi", 42, [1]) would make content.get raise
        # AttributeError and crash the consumer; treat any malformed frame as a no-op.
        if not isinstance(content, dict):
            return
        if content.get("type") == "pong":
            self._missed_pings = 0
            return
        # Subclasses may override to handle other inbound messages; default ignore.

    # -- teardown ---------------------------------------------------------
    async def _discard_groups(self) -> None:
        for group in list(self._groups):
            try:
                await self.channel_layer.group_discard(group, self.channel_name)
            except Exception:
                # Cleanup is best-effort when Redis/the channel layer is down.
                # Never let one failed discard prevent the connection lease from
                # being released or the socket from being closed; Channels group
                # membership has its own bounded expiry for crash recovery.
                logger.warning("WebSocket group cleanup failed.", exc_info=True)
        self._groups.clear()

    async def disconnect(self, code):
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        await self._discard_groups()
        await self._release_connection_slot()

    # -- helpers ----------------------------------------------------------
    def _authed_user(self):
        """Return the authenticated user or None (AnonymousUser -> None)."""
        user = self.scope.get("user")
        if user is None or isinstance(user, AnonymousUser):
            return None
        return user

    def _schema(self) -> str | None:
        tenant = self.scope.get("tenant")
        return tenant.schema_name if tenant is not None else None


class PingConsumer(HeartbeatConsumerMixin):
    """Authenticated smoke consumer that inherits live-session enforcement."""

    async def connect(self):
        user = self.scope.get("user")
        if isinstance(user, AnonymousUser):
            await self.close(code=4401)
            return
        if not await self.claim_connection_slot():
            await self.close(code=self.connection_slot_denial_code())
            return
        try:
            await self.accept(subprotocol=accepted_subprotocol(self.scope))
            await self.send_json({"type": "hello", "user_id": user.pk})
            await self.start_heartbeat()
        except Exception:
            await self._close_with_cleanup(CLOSE_INTERNAL)

    async def receive_json(self, content, **kwargs):
        if not isinstance(content, dict):
            return
        if content.get("type") == "pong":
            await super().receive_json(content, **kwargs)
            return
        if content.get("type") == "ping":
            await self.send_json({"type": "pong"})
