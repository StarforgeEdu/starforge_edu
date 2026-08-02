from __future__ import annotations

import pytest
from django.test import override_settings

from infrastructure.websocket.consumers import (
    APPLICATION_SUBPROTOCOL,
    CLOSE_INTERNAL,
    CLOSE_RATE_LIMITED,
    CLOSE_TOO_LARGE,
    INBOUND_RATE_LIMIT,
    MAX_INBOUND_BYTES,
    HeartbeatConsumerMixin,
    accepted_subprotocol,
)
from infrastructure.websocket.groups import (
    cohort_attendance_group,
    messaging_thread_group,
    notification_principal_group,
    user_group,
)
from infrastructure.websocket.middleware import (
    TenantAwareAuthMiddleware,
    _canonical_host,
    _extract_credential,
    _origin_allowed,
)

TOKEN = "A" * 54


def _scope(**overrides):
    scope = {
        "headers": [(b"host", b"tenant.example.test")],
        "scheme": "wss",
        "query_string": b"",
        "subprotocols": [],
    }
    scope.update(overrides)
    return scope


@override_settings(ALLOWED_HOSTS=["tenant.example.test"])
def test_host_parser_rejects_duplicates_and_invalid_hosts():
    assert _canonical_host(_scope()) == "tenant.example.test"
    assert _canonical_host(_scope(headers=[(b"host", b"evil.example")])) is None
    assert (
        _canonical_host(_scope(headers=[(b"host", b"tenant.example.test"), (b"host", b"evil.example")]))
        is None
    )


def test_origin_is_exact_and_wildcard_free():
    exact = _scope(headers=[(b"host", b"tenant.example.test"), (b"origin", b"https://tenant.example.test")])
    assert _origin_allowed(exact, "tenant.example.test") == (True, True)
    hostile = _scope(headers=[(b"host", b"tenant.example.test"), (b"origin", b"https://evil.example")])
    assert _origin_allowed(hostile, "tenant.example.test") == (False, True)
    malformed = _scope(headers=[(b"host", b"tenant.example.test"), (b"origin", b"null")])
    assert _origin_allowed(malformed, "tenant.example.test") == (False, True)


@override_settings(WEBSOCKET_ALLOWED_ORIGINS=["https://console.example.test"])
def test_explicit_cross_origin_allowlist_is_exact():
    scope = _scope(
        headers=[
            (b"host", b"tenant.example.test"),
            (b"origin", b"https://console.example.test"),
        ]
    )
    assert _origin_allowed(scope, "tenant.example.test") == (True, True)


def test_forbidden_query_token_rejects_even_with_bearer_subprotocol():
    credential = _extract_credential(
        _scope(query_string=f"token={TOKEN}".encode(), subprotocols=[f"bearer.{TOKEN}"]),
        origin_present=False,
        origin_allowed=True,
    )
    assert credential.invalid is True


def test_ambiguous_or_malformed_tokens_are_rejected():
    duplicate = _extract_credential(
        _scope(subprotocols=[f"bearer.{TOKEN}", f"bearer.{'B' * 54}"]),
        origin_present=False,
        origin_allowed=True,
    )
    short = _extract_credential(
        _scope(subprotocols=["bearer.short"]),
        origin_present=False,
        origin_allowed=True,
    )
    mixed = _extract_credential(
        _scope(query_string=f"token={TOKEN}".encode(), subprotocols=[f"bearer.{TOKEN}"]),
        origin_present=False,
        origin_allowed=True,
    )
    assert duplicate.invalid is True
    assert short.invalid is True
    assert mixed.invalid is True


@override_settings(API_SESSION_COOKIE_NAME="starforge_session")
def test_cookie_credential_requires_browser_origin():
    scope = _scope(
        headers=[(b"host", b"tenant.example.test"), (b"cookie", f"starforge_session={TOKEN}".encode())]
    )
    missing_origin = _extract_credential(scope, origin_present=False, origin_allowed=True)
    hostile_origin = _extract_credential(scope, origin_present=True, origin_allowed=False)
    valid_origin = _extract_credential(scope, origin_present=True, origin_allowed=True)
    assert missing_origin.invalid is True
    assert hostile_origin.invalid is True
    assert valid_origin == valid_origin.__class__(token=TOKEN, transport="cookie")


@override_settings(API_SESSION_COOKIE_NAME="starforge_session")
def test_duplicate_cookie_or_protocol_headers_are_rejected():
    duplicate_cookie = _scope(
        headers=[
            (b"host", b"tenant.example.test"),
            (b"cookie", f"starforge_session={TOKEN}; starforge_session={'B' * 54}".encode()),
        ]
    )
    duplicate_protocol = _scope(
        headers=[
            (b"host", b"tenant.example.test"),
            (b"sec-websocket-protocol", f"bearer.{TOKEN}".encode()),
            (b"sec-websocket-protocol", f"bearer.{'B' * 54}".encode()),
        ],
        subprotocols=[f"bearer.{TOKEN}"],
    )
    assert (
        _extract_credential(
            duplicate_cookie,
            origin_present=True,
            origin_allowed=True,
        ).invalid
        is True
    )
    assert (
        _extract_credential(
            duplicate_protocol,
            origin_present=False,
            origin_allowed=True,
        ).invalid
        is True
    )


def test_protocol_header_and_asgi_scope_must_match_exactly():
    scope = _scope(
        headers=[
            (b"host", b"tenant.example.test"),
            (b"sec-websocket-protocol", f"bearer.{TOKEN}, starforge.v1".encode()),
        ],
        subprotocols=["starforge.v1", f"bearer.{TOKEN}"],
    )
    assert _extract_credential(
        scope,
        origin_present=False,
        origin_allowed=True,
    ).invalid


def test_bearer_credential_is_never_echoed_as_subprotocol():
    scope = _scope(subprotocols=[f"bearer.{TOKEN}"])
    assert accepted_subprotocol(scope) is None
    scope["subprotocols"] = [APPLICATION_SUBPROTOCOL, f"bearer.{TOKEN}"]
    assert accepted_subprotocol(scope) == APPLICATION_SUBPROTOCOL


@pytest.mark.asyncio
@override_settings(ALLOWED_HOSTS=["tenant.example.test"])
async def test_middleware_discards_raw_key_after_handshake(monkeypatch):
    from types import SimpleNamespace

    import infrastructure.websocket.middleware as ws_middleware

    captured: dict = {}

    async def inner(scope, receive, send):
        captured.update(scope)

    async def not_limited(**kwargs) -> bool:
        return False

    async def tenant_for_host(hostname):
        return SimpleNamespace(schema_name="tenant_a")

    async def session_for_key(raw_token, tenant):
        assert raw_token == TOKEN
        return SimpleNamespace(pk=7), False, 91, False, "staff", 3, False

    monkeypatch.setattr(ws_middleware, "_rate_limited", not_limited)
    monkeypatch.setattr(ws_middleware, "_resolve_tenant_by_hostname", tenant_for_host)
    monkeypatch.setattr(ws_middleware, "_session_from_key", session_for_key)
    middleware = TenantAwareAuthMiddleware(inner)
    await middleware(
        _scope(client=("192.0.2.1", 1234), subprotocols=[f"bearer.{TOKEN}"]),
        None,
        None,
    )
    assert captured["_ws_session_id"] == 91
    assert captured["_ws_auth_transport"] == "subprotocol"
    assert captured["principal_kind"] == "staff"
    assert captured["principal_id"] == 3
    assert "_ws_token" not in captured
    assert TOKEN not in repr(captured)


def test_group_names_reject_public_schema_and_injection():
    assert user_group("tenant_a", 7) == "tenant_a.user.7"
    assert cohort_attendance_group("tenant_a", "9") == "tenant_a.cohort.9"
    assert notification_principal_group("tenant_a", "staff", 11) == "tenant_a.n.staff.11"
    assert messaging_thread_group("tenant_a", 13) == "tenant_a.m.t.13"
    with pytest.raises(ValueError, match="tenant schema"):
        user_group("public", 7)
    with pytest.raises(ValueError, match="tenant schema"):
        user_group("tenant.a", 7)
    with pytest.raises(ValueError, match="positive integer"):
        cohort_attendance_group("tenant_a", "9.evil")
    with pytest.raises(ValueError, match="positive integer"):
        cohort_attendance_group("tenant_a", 0)
    with pytest.raises(ValueError, match="role-native principal"):
        notification_principal_group("tenant_a", "staff.evil", 1)


def test_notification_group_stays_within_channels_hard_limit():
    schema = "a" + ("b" * 62)
    group = notification_principal_group(schema, "teacher", 9_223_372_036_854_775_807)
    assert len(group) <= 100


@pytest.mark.asyncio
async def test_oversized_inbound_frame_closes_and_cleans_up():
    consumer = HeartbeatConsumerMixin()
    events: list[object] = []

    async def discard_groups() -> None:
        events.append("discard")

    async def close(*, code: int) -> None:
        events.append(code)

    consumer._discard_groups = discard_groups  # type: ignore[method-assign]
    consumer.close = close  # type: ignore[method-assign]
    await consumer.receive(text_data="x" * (MAX_INBOUND_BYTES + 1))
    assert events == ["discard", CLOSE_TOO_LARGE]


@pytest.mark.asyncio
async def test_inbound_control_frame_flood_is_closed():
    consumer = HeartbeatConsumerMixin()
    events: list[object] = []

    async def discard_groups() -> None:
        events.append("discard")

    async def close(*, code: int) -> None:
        events.append(code)

    consumer._discard_groups = discard_groups  # type: ignore[method-assign]
    consumer.close = close  # type: ignore[method-assign]
    for _ in range(INBOUND_RATE_LIMIT + 1):
        await consumer.receive(text_data='{"type":"pong"}')
    assert events == ["discard", CLOSE_RATE_LIMITED]


@pytest.mark.asyncio
async def test_oversized_outbound_event_is_dropped():
    consumer = HeartbeatConsumerMixin()
    sent: list[dict] = []

    async def authorized() -> bool:
        return True

    async def send_json(value: dict) -> None:
        sent.append(value)

    consumer._reauthorize = authorized  # type: ignore[method-assign]
    consumer.send_json = send_json  # type: ignore[method-assign]
    result = await consumer.send_bounded_event(
        event_type="notification",
        payload={"body": "x" * (MAX_INBOUND_BYTES + 1)},
    )
    assert result is False
    assert sent == []


@pytest.mark.asyncio
async def test_channel_layer_cleanup_failure_does_not_abandon_remaining_groups():
    consumer = HeartbeatConsumerMixin()
    attempted: set[str] = set()

    class FailingLayer:
        async def group_discard(self, group: str, channel_name: str) -> None:
            attempted.add(group)
            if group == "tenant_a.group.one":
                raise ConnectionError("redis unavailable")

    consumer.channel_layer = FailingLayer()
    consumer.channel_name = "test-channel"
    consumer._groups = {"tenant_a.group.one", "tenant_a.group.two"}

    await consumer._discard_groups()

    assert attempted == {"tenant_a.group.one", "tenant_a.group.two"}
    assert consumer._groups == set()


@pytest.mark.asyncio
@override_settings(
    WEBSOCKET_MAX_CONNECTIONS_PER_SESSION=2,
    WEBSOCKET_CONNECTION_LEASE_SECONDS=90,
)
async def test_connection_slots_are_bounded_and_released():
    consumers = []
    for index in range(3):
        consumer = HeartbeatConsumerMixin()
        consumer.scope = {"tenant": type("Tenant", (), {"schema_name": "tenant_a"})(), "_ws_session_id": 7}
        consumer.channel_name = f"channel-{index}"
        consumers.append(consumer)

    assert await consumers[0].claim_connection_slot() is True
    assert await consumers[1].claim_connection_slot() is True
    assert await consumers[2].claim_connection_slot() is False
    await consumers[0]._release_connection_slot()
    assert await consumers[2].claim_connection_slot() is True
    await consumers[1]._release_connection_slot()
    await consumers[2]._release_connection_slot()


@pytest.mark.asyncio
async def test_connection_slot_dependency_failure_is_internal_not_saturation(monkeypatch):
    import infrastructure.websocket.consumers as websocket_consumers

    async def unavailable(**kwargs):
        raise ConnectionError("cache unavailable")

    monkeypatch.setattr(websocket_consumers, "_claim_connection_lease", unavailable)
    consumer = HeartbeatConsumerMixin()
    consumer.scope = {
        "tenant": type("Tenant", (), {"schema_name": "tenant_a"})(),
        "_ws_session_id": 7,
    }
    consumer.channel_name = "channel-1"

    assert await consumer.claim_connection_slot() is False
    assert consumer.connection_slot_denial_code() == CLOSE_INTERNAL


@pytest.mark.asyncio
@override_settings(WEBSOCKET_CONNECTION_LEASE_SECONDS=90)
async def test_stale_connection_cannot_refresh_or_delete_reassigned_redis_lease(monkeypatch):
    import infrastructure.websocket.consumers as websocket_consumers

    class OwnedLeaseRedis:
        values = {"lease": "new-channel"}
        expiries: list[tuple[str, int]] = []

        def eval(self, script, _keys, key, expected, *args):
            if self.values.get(key) != expected:
                return 0
            if "expire" in script:
                self.expiries.append((key, int(args[0])))
                return 1
            self.values.pop(key, None)
            return 1

    redis = OwnedLeaseRedis()
    monkeypatch.setattr(websocket_consumers, "_redis_connection_lease_client", lambda: redis)

    assert await websocket_consumers._refresh_connection_lease("lease", "old-channel") is False
    await websocket_consumers._release_connection_lease("lease", "old-channel")
    assert redis.values == {"lease": "new-channel"}

    assert await websocket_consumers._refresh_connection_lease("lease", "new-channel") is True
    assert redis.expiries == [("lease", 90)]
    await websocket_consumers._release_connection_lease("lease", "new-channel")
    assert redis.values == {}
