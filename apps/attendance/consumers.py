"""Attendance WebSocket consumer (D4-LC-4).

``ws/cohorts/<cohort_id>/attendance/`` — live attendance dashboard for one
cohort. The feed is cohort-WIDE (every student's live marks), so it is a STAFF
feed. Authorization is checked **on connect** (not per-message):

  1. The user must hold ``attendance:read`` (``has_permission_code``).
  2. AND be a director (``*:*``), an HoD whose branch/department membership covers
     the cohort, or one of the cohort's actual teachers. A STUDENT/PARENT also holds ``attendance:read`` but
     only ROW-scoped to self / their children (``apps.attendance.selectors``), so
     they must NOT receive the whole cohort's live marks; a teacher from another
     branch must not watch this cohort either.

Failure modes:
  - anonymous / cross-tenant / stale tv -> 4401 (middleware yields AnonymousUser)
  - authenticated but not permitted (no attendance:read, or wrong branch) -> 4403
  - unknown cohort -> 4403 (no information leak about which cohorts exist)

On success the socket joins ``f"{schema}.cohort.{cohort_id}"`` — the group the
attendance producer (``apps.notifications.services.push_cohort_attendance``,
driven by ``dispatch()`` via the attendance receiver) writes to. The schema
prefix mirrors the user/branch groups (shared-Redis tenant isolation).

Handler ``attendance_update`` relays the producer payload to the socket as
``{"type": "attendance.update", "payload": {...}}``.
"""

from __future__ import annotations

from channels.db import database_sync_to_async
from django_tenants.utils import schema_context

from core.permissions import get_user_roles, has_permission_code
from infrastructure.websocket.consumers import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    HeartbeatConsumerMixin,
    accepted_subprotocol,
)
from infrastructure.websocket.groups import cohort_attendance_group


@database_sync_to_async
def _can_watch_cohort(
    *,
    schema: str,
    user_id: int,
    cohort_id: int,
    principal_kind: str,
    principal_id: int | None,
) -> bool:
    """Apply the HTTP dashboard's branch/department/teaching scope on connect."""
    from apps.users.models import User

    with schema_context(schema):
        user = User.objects.filter(pk=user_id, is_active=True).first()
        if user is None:
            return False
        request_context = type(
            "AttendanceScopeRequest",
            (),
            {
                "user": user,
                "principal_kind": principal_kind,
                "principal_id": principal_id,
                "principal_validated": True,
            },
        )()
        roles = get_user_roles(request_context)
        if not user.is_superuser and not has_permission_code(roles, "attendance:read"):
            return False
        from apps.attendance.selectors import scoped_dashboard_cohorts

        # Reuse the HTTP dashboard's single canonical object scope so WebSocket
        # and request authorization cannot drift apart.
        return scoped_dashboard_cohorts(user=user, roles=roles).filter(pk=cohort_id).exists()


class AttendanceConsumer(HeartbeatConsumerMixin):
    async def connect(self):
        user = self._authed_user()
        schema = self._schema()
        if user is None or schema is None:
            await self.close(code=CLOSE_UNAUTHORIZED)
            return

        try:
            cohort_id = int(self.scope["url_route"]["kwargs"]["cohort_id"])
        except (KeyError, ValueError, TypeError):
            await self.close(code=CLOSE_FORBIDDEN)
            return

        if not await _can_watch_cohort(
            schema=schema,
            user_id=user.pk,
            cohort_id=cohort_id,
            principal_kind=str(self.scope.get("principal_kind") or ""),
            principal_id=self.scope.get("principal_id"),
        ):
            await self.close(code=CLOSE_FORBIDDEN)
            return
        if not await self.claim_connection_slot():
            await self.close(code=self.connection_slot_denial_code())
            return

        self._cohort_id = cohort_id  # remembered so the heartbeat can re-check scope (R1-05)
        try:
            await self.accept(subprotocol=accepted_subprotocol(self.scope))
            await self.join_group(cohort_attendance_group(schema, cohort_id))
            await self.start_heartbeat()
        except Exception:
            await self._close_with_cleanup(1011)

    async def _still_authorized(self) -> bool:
        """R1-05: re-run the connect-time branch/role gate each heartbeat, so a teacher
        whose role or branch membership is revoked mid-session is dropped (close 4403),
        not left watching the cohort's live marks."""
        user = self._authed_user()
        schema = self._schema()
        if user is None or schema is None:
            return False
        return await _can_watch_cohort(
            schema=schema,
            user_id=user.pk,
            cohort_id=self._cohort_id,
            principal_kind=str(self.scope.get("principal_kind") or ""),
            principal_id=self.scope.get("principal_id"),
        )

    async def attendance_update(self, event: dict) -> None:
        """Relay a producer payload (group_send type ``attendance.update``)."""
        allowed = ("record_id", "student_id", "lesson_id", "status", "auto")
        payload = {key: event[key] for key in allowed if key in event}
        await self.send_bounded_event(event_type="attendance.update", payload=payload)
