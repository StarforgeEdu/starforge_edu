"""Notification WebSocket consumer (D4-LC-3).

``ws/notifications/`` — any authenticated user. On connect the socket joins:

  - ``f"{schema}.user.{user_id}"`` — the per-user fan-out group that
    ``apps.notifications.services.push_in_app`` (the in-app channel of
    ``dispatch()``) writes to. The schema prefix is mandatory: user ids are
    per-tenant autoincrements, so an unscoped ``user.5`` collides across tenants
    on the shared Redis channel layer. This MATCHES the producer scheme in
    ``celery_tasks/notification_tasks._deliver_in_app`` (the code is the source
    of truth — DAY-4.md's "user.{id}" predates the Day-3 schema-prefix fix).
  - ``f"{schema}.branch.{branch_id}"`` for every active (non-revoked)
    RoleMembership — branch-wide broadcasts (announcements) reach staff here.

Handler ``notification_message`` relays the producer payload to the socket as
``{"type": "notification", "payload": {...}}``. The relayed envelope strips the
channel-layer ``"type"`` routing key and forwards the remaining fields.

Anonymous / cross-tenant / stale-tv connections never reach here as a real user
(the middleware yields AnonymousUser); the consumer closes 4401.
"""

from __future__ import annotations

from channels.db import database_sync_to_async
from django_tenants.utils import schema_context

from infrastructure.websocket.consumers import (
    CLOSE_UNAUTHORIZED,
    HeartbeatConsumerMixin,
    accepted_subprotocol,
)


@database_sync_to_async
def _active_branch_ids(*, schema: str, user_id: int) -> list[int]:
    """Branch ids of the user's active (non-revoked) RoleMemberships."""
    from apps.users.models import RoleMembership

    with schema_context(schema):
        return list(
            RoleMembership.objects.filter(user_id=user_id, revoked_at__isnull=True)
            .values_list("branch_id", flat=True)
            .distinct()
        )


class NotificationConsumer(HeartbeatConsumerMixin):
    async def connect(self):
        user = self._authed_user()
        schema = self._schema()
        if user is None or schema is None:
            await self.close(code=CLOSE_UNAUTHORIZED)
            return

        await self.accept(subprotocol=accepted_subprotocol(self.scope))
        await self.join_group(f"{schema}.user.{user.pk}")
        for branch_id in await _active_branch_ids(schema=schema, user_id=user.pk):
            await self.join_group(f"{schema}.branch.{branch_id}")
        await self.start_heartbeat()

    async def _still_authorized(self) -> bool:
        """R1-05: reconcile the per-branch subscriptions each heartbeat. A branch membership
        revoked mid-session must stop receiving that branch's announcements (leave the group),
        and a newly-granted branch should start. The session itself is already re-validated by
        the mixin, and the user keeps their per-user feed, so the socket stays open (True) —
        only the branch-group set is adjusted."""
        user = self._authed_user()
        schema = self._schema()
        if user is None or schema is None:
            return False
        prefix = f"{schema}.branch."
        want = {f"{prefix}{bid}" for bid in await _active_branch_ids(schema=schema, user_id=user.pk)}
        have = {g for g in self._groups if g.startswith(prefix)}
        for stale in have - want:  # membership revoked -> stop the branch broadcasts
            await self.channel_layer.group_discard(stale, self.channel_name)
            self._groups.discard(stale)
        for fresh in want - have:  # newly-granted branch -> start receiving
            await self.join_group(fresh)
        return True

    async def notification_message(self, event: dict) -> None:
        """Relay a producer payload (group_send type ``notification.message``)."""
        payload = {k: v for k, v in event.items() if k != "type"}
        await self.send_json({"type": "notification", "payload": payload})
