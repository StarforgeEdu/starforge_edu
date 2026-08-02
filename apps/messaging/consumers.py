"""Exact-principal, recoverable WebSocket stream for one messaging thread.

The channel layer carries pointer events only.  Message bodies and attachment
references remain in participant-scoped HTTP resources, while the append-only
database event log supplies ordering, deduplication, and reconnect recovery.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from channels.db import database_sync_to_async
from django_tenants.utils import schema_context

from apps.messaging.interfaces.services import IThreadService
from apps.messaging.models import Thread, ThreadRealtimeEvent
from apps.messaging.presenters import thread_event_page_to_dict
from core.container import container
from core.exceptions import ValidationException
from infrastructure.websocket.consumers import (
    CLOSE_FORBIDDEN,
    CLOSE_INTERNAL,
    CLOSE_RATE_LIMITED,
    CLOSE_UNAUTHORIZED,
    HeartbeatConsumerMixin,
    accepted_subprotocol,
)
from infrastructure.websocket.groups import messaging_thread_group

_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
_SYNC_LIMIT_DEFAULT = 50
_SYNC_LIMIT_MAX = 50
_SYNC_RATE_LIMIT = 6
_SYNC_RATE_WINDOW = 10.0
_PRINCIPAL_KINDS = frozenset({"student", "teacher", "parent", "staff"})


def _service() -> IThreadService:
    return container.resolve(IThreadService)  # type: ignore[type-abstract]


@database_sync_to_async
def _stream_metadata(
    *,
    schema: str,
    thread_id: int,
    user,
    principal_kind: str,
    principal_id: int,
) -> dict[str, int] | None:
    with schema_context(schema):
        service = _service()
        if not service.can_stream_thread(
            thread_id=thread_id,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
        ):
            return None
        thread = Thread.objects.filter(pk=thread_id).only("id", "realtime_sequence").first()
        if thread is None:
            return None
        page = service.event_page(thread=thread, after=thread.realtime_sequence, limit=1)
        return {
            "high_watermark": page.high_watermark,
            "recovery_floor": page.recovery_floor,
        }


@database_sync_to_async
def _stream_authorized(
    *,
    schema: str,
    thread_id: int,
    user,
    principal_kind: str,
    principal_id: int,
) -> bool:
    with schema_context(schema):
        return _service().can_stream_thread(
            thread_id=thread_id,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )


@database_sync_to_async
def _recover_events(
    *,
    schema: str,
    thread_id: int,
    user,
    principal_kind: str,
    principal_id: int,
    after: int,
    limit: int,
) -> tuple[str, dict[str, Any] | None]:
    """Return ``(status, payload)`` without crossing domain exceptions into ASGI."""

    with schema_context(schema):
        service = _service()
        if not service.can_stream_thread(
            thread_id=thread_id,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
        ):
            return "forbidden", None
        thread = Thread.objects.filter(pk=thread_id).only("id", "realtime_sequence").first()
        if thread is None:
            return "forbidden", None
        try:
            page = service.event_page(thread=thread, after=after, limit=limit)
        except ValidationException as exc:
            return str(exc.code or "invalid_event_cursor"), None
        return "ok", thread_event_page_to_dict(page, thread_id=thread_id)


@database_sync_to_async
def _canonical_event_pointer(*, schema: str, thread_id: int, sequence: int) -> dict[str, Any] | None:
    """Resolve a channel-layer hint back to immutable database evidence.

    The channel layer is an acceleration path, not an authority. Re-reading the
    exact cursor prevents a malformed or stale internal producer payload from
    inventing actor/message identifiers for a connected client.
    """

    with schema_context(schema):
        row = (
            ThreadRealtimeEvent.objects.filter(thread_id=thread_id, sequence=sequence)
            .values(
                "thread_id",
                "sequence",
                "kind",
                "message_id",
                "actor_principal_kind",
                "actor_principal_id",
                "created_at",
            )
            .first()
        )
        if row is None:
            return None
        return {
            "thread_id": int(row["thread_id"]),
            "sequence": int(row["sequence"]),
            "kind": str(row["kind"]),
            "message_id": int(row["message_id"]),
            "actor_principal_kind": str(row["actor_principal_kind"]),
            "actor_principal_id": int(row["actor_principal_id"]),
            "created_at": row["created_at"].isoformat(),
        }


class ThreadConsumer(HeartbeatConsumerMixin):
    """Receive a pointer stream for exactly one thread participant principal."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._thread_id: int | None = None
        self._principal_kind = ""
        self._principal_id: int | None = None
        self._sync_times: deque[float] = deque(maxlen=_SYNC_RATE_LIMIT)

    async def connect(self) -> None:
        user = self._authed_user()
        schema = self._schema()
        raw_thread_id = self.scope.get("url_route", {}).get("kwargs", {}).get("thread_id")
        principal_kind = str(self.scope.get("principal_kind") or "")
        principal_id = self.scope.get("principal_id")
        if user is None or schema is None:
            await self.close(code=CLOSE_UNAUTHORIZED)
            return
        if (
            isinstance(raw_thread_id, bool)
            or not isinstance(raw_thread_id, int)
            or not 1 <= raw_thread_id <= _MAX_SIGNED_BIGINT
            or principal_kind not in _PRINCIPAL_KINDS
            or isinstance(principal_id, bool)
            or not isinstance(principal_id, int)
            or principal_id <= 0
        ):
            await self.close(code=CLOSE_FORBIDDEN)
            return

        self._thread_id = raw_thread_id
        self._principal_kind = principal_kind
        self._principal_id = principal_id
        try:
            metadata = await _stream_metadata(
                schema=schema,
                thread_id=raw_thread_id,
                user=user,
                principal_kind=principal_kind,
                principal_id=principal_id,
            )
        except Exception:
            await self.close(code=CLOSE_INTERNAL)
            return
        if metadata is None:
            await self.close(code=CLOSE_FORBIDDEN)
            return
        if not await self.claim_connection_slot():
            await self.close(code=self.connection_slot_denial_code())
            return

        try:
            await self.accept(subprotocol=accepted_subprotocol(self.scope))
            await self.join_group(messaging_thread_group(schema, raw_thread_id))
            sent = await self.send_bounded_event(
                event_type="thread.ready",
                payload={
                    "protocol": "starforge.messaging.thread.v1",
                    "thread_id": raw_thread_id,
                    **metadata,
                    "max_sync_events": _SYNC_LIMIT_MAX,
                    "event_delivery": "best_effort_pointer_with_durable_recovery",
                    "live_ordering": "not_guaranteed",
                    "recovery_ordering": "sequence_ascending",
                    "deduplication_key": "sequence",
                    "gap_recovery": "thread.sync",
                    "messages_url": f"/api/v1/messaging/threads/{raw_thread_id}/messages/",
                    "recovery_url": f"/api/v1/messaging/threads/{raw_thread_id}/events/",
                    "capabilities": {
                        "missed_event_recovery": True,
                        "read_receipts": True,
                        "typing": False,
                        "delivery_receipts": False,
                        "presence": "not_provided",
                    },
                },
            )
            if sent:
                await self.start_heartbeat()
        except Exception:
            await self._close_with_cleanup(CLOSE_INTERNAL)

    async def _still_authorized(self) -> bool:
        user = self._authed_user()
        schema = self._schema()
        if user is None or schema is None or self._thread_id is None or self._principal_id is None:
            return False
        return await _stream_authorized(
            schema=schema,
            thread_id=self._thread_id,
            user=user,
            principal_kind=self._principal_kind,
            principal_id=self._principal_id,
        )

    async def receive_json(self, content, **kwargs) -> None:
        if not isinstance(content, dict):
            return
        if content.get("type") == "pong":
            await super().receive_json(content, **kwargs)
            return
        if content.get("type") != "thread.sync":
            await self._protocol_error("unsupported_command")
            return
        if set(content) - {"type", "after", "limit"}:
            await self._protocol_error("invalid_sync_request")
            return
        after = content.get("after")
        limit = content.get("limit", _SYNC_LIMIT_DEFAULT)
        if (
            isinstance(after, bool)
            or not isinstance(after, int)
            or not 0 <= after <= _MAX_SIGNED_BIGINT
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _SYNC_LIMIT_MAX
        ):
            await self._protocol_error("invalid_sync_request")
            return
        now = time.monotonic()
        while self._sync_times and now - self._sync_times[0] >= _SYNC_RATE_WINDOW:
            self._sync_times.popleft()
        if len(self._sync_times) >= _SYNC_RATE_LIMIT:
            await self._close_with_cleanup(CLOSE_RATE_LIMITED)
            return
        self._sync_times.append(now)

        if not await self._reauthorize():
            return
        user = self._authed_user()
        schema = self._schema()
        if user is None or schema is None or self._thread_id is None or self._principal_id is None:
            await self._close_with_cleanup(CLOSE_UNAUTHORIZED)
            return
        try:
            status, payload = await _recover_events(
                schema=schema,
                thread_id=self._thread_id,
                user=user,
                principal_kind=self._principal_kind,
                principal_id=self._principal_id,
                after=after,
                limit=limit,
            )
        except Exception:
            await self._close_with_cleanup(CLOSE_INTERNAL)
            return
        if status == "forbidden":
            await self._close_with_cleanup(CLOSE_FORBIDDEN)
            return
        if status != "ok" or payload is None:
            await self._protocol_error(status)
            return
        await self.send_bounded_event(event_type="thread.sync", payload=payload)

    async def _protocol_error(self, code: str) -> None:
        await self.send_bounded_event(
            event_type="protocol.error",
            payload={"code": code},
        )

    async def messaging_thread_event(self, event: dict) -> None:
        """Relay one canonical durable pointer; never trust producer fields."""

        thread_id = event.get("thread_id")
        sequence = event.get("sequence")
        if (
            thread_id != self._thread_id
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 1 <= sequence <= _MAX_SIGNED_BIGINT
        ):
            return
        schema = self._schema()
        if schema is None:
            await self._close_with_cleanup(CLOSE_UNAUTHORIZED)
            return
        try:
            payload = await _canonical_event_pointer(
                schema=schema,
                thread_id=thread_id,
                sequence=sequence,
            )
        except Exception:
            await self._close_with_cleanup(CLOSE_INTERNAL)
            return
        if payload is None:
            return
        await self.send_bounded_event(event_type="thread.event", payload=payload)
