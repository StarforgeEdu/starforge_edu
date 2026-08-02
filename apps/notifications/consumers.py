"""Notification WebSocket consumer (D4-LC-3).

``ws/notifications/`` — any authenticated role account. On connect the socket
joins only ``f"{schema}.n.{principal_kind}.{principal_id}"``. The tenant prefix
prevents collisions on the shared channel layer, while the role-native principal
pair prevents a bridge ``User`` that backs multiple accounts from merging their
private feeds. ``apps.notifications.services.push_in_app`` uses the same canonical
group helper.
Handler ``notification_message`` relays the producer payload to the socket as
``{"type": "notification", "payload": {...}}``. The relayed envelope strips the
channel-layer ``"type"`` routing key and forwards the remaining fields.

Anonymous / cross-tenant / stale-tv connections never reach here as a real user
(the middleware yields AnonymousUser); the consumer closes 4401.
"""

from __future__ import annotations

from channels.db import database_sync_to_async
from django_tenants.utils import schema_context

from apps.notifications.principals import resolve_recipient_principal
from infrastructure.websocket.consumers import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    HeartbeatConsumerMixin,
    accepted_subprotocol,
)
from infrastructure.websocket.groups import notification_principal_group


@database_sync_to_async
def _notification_principal(
    *, schema: str, user_id: int, principal_kind: str, principal_id: object
) -> tuple[str, int] | None:
    with schema_context(schema):
        principal = resolve_recipient_principal(
            user_id=user_id,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )
        if not principal.is_deliverable or principal.kind is None or principal.principal_id is None:
            return None
        return principal.kind, principal.principal_id


class NotificationConsumer(HeartbeatConsumerMixin):
    async def connect(self):
        user = self._authed_user()
        schema = self._schema()
        if user is None or schema is None:
            await self.close(code=CLOSE_UNAUTHORIZED)
            return
        principal = await _notification_principal(
            schema=schema,
            user_id=user.pk,
            principal_kind=str(self.scope.get("principal_kind") or ""),
            principal_id=self.scope.get("principal_id"),
        )
        if principal is None:
            await self.close(code=CLOSE_FORBIDDEN)
            return
        self._recipient_principal_kind, self._recipient_principal_id = principal
        if not await self.claim_connection_slot():
            await self.close(code=self.connection_slot_denial_code())
            return

        try:
            await self.accept(subprotocol=accepted_subprotocol(self.scope))
            await self.join_group(
                notification_principal_group(
                    schema,
                    self._recipient_principal_kind,
                    self._recipient_principal_id,
                )
            )
            await self.start_heartbeat()
        except Exception:
            await self._close_with_cleanup(1011)

    async def _still_authorized(self) -> bool:
        user = self._authed_user()
        schema = self._schema()
        if user is None or schema is None:
            return False
        principal = await _notification_principal(
            schema=schema,
            user_id=user.pk,
            principal_kind=str(self.scope.get("principal_kind") or ""),
            principal_id=self.scope.get("principal_id"),
        )
        return principal == (
            getattr(self, "_recipient_principal_kind", None),
            getattr(self, "_recipient_principal_id", None),
        )

    async def notification_message(self, event: dict) -> None:
        """Relay a producer payload (group_send type ``notification.message``)."""
        if event.get("recipient_principal_kind") != getattr(
            self, "_recipient_principal_kind", None
        ) or event.get("recipient_principal_id") != getattr(self, "_recipient_principal_id", None):
            return
        allowed = ("id", "event_type", "title", "body", "data", "created_at")
        payload = {key: event[key] for key in allowed if key in event}
        await self.send_bounded_event(event_type="notification", payload=payload)
