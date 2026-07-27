"""Attendance WebSocket consumer (D4-LC-4).

``ws/cohorts/<cohort_id>/attendance/`` — live attendance dashboard for one
cohort. The feed is cohort-WIDE (every student's live marks), so it is a STAFF
feed. Authorization is checked **on connect** (not per-message):

  1. The user must hold ``attendance:read`` (``has_permission_code``).
  2. AND be a director (``*:*``) OR hold a DASHBOARD role (head-of-dept / teacher)
     in the cohort's branch. A STUDENT/PARENT also holds ``attendance:read`` but
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

from core.permissions import Role, has_permission_code
from infrastructure.websocket.consumers import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    HeartbeatConsumerMixin,
    accepted_subprotocol,
)

# Roles that may watch the cohort-WIDE live dashboard (director is handled separately as
# "sees every cohort"). STUDENT/PARENT are excluded: their attendance:read is row-scoped to
# self / their children in the HTTP selectors, so they must not get every peer's live marks.
_DASHBOARD_ROLES = frozenset({Role.HEAD_OF_DEPT, Role.TEACHER})


@database_sync_to_async
def _can_watch_cohort(*, schema: str, user_id: int, cohort_id: int) -> bool:
    """attendance:read AND (director OR a DASHBOARD-role membership in the cohort's branch)."""
    from apps.cohorts.models import Cohort
    from apps.users.models import RoleMembership

    with schema_context(schema):
        memberships = list(
            RoleMembership.objects.filter(user_id=user_id, revoked_at__isnull=True).values_list(
                "role", "branch_id"
            )
        )
        roles = {role for role, _branch_id in memberships}
        if not has_permission_code(roles, "attendance:read"):
            return False
        if Role.DIRECTOR in roles:
            return True
        cohort_branch_id = Cohort.objects.filter(pk=cohort_id).values_list("branch_id", flat=True).first()
        if cohort_branch_id is None:
            return False
        # A row-scoped student/parent must NOT receive the whole cohort's feed: require a
        # dashboard-role (HOD/teacher) membership in the cohort's own branch.
        return any(
            role in _DASHBOARD_ROLES and branch_id == cohort_branch_id
            for role, branch_id in memberships
        )


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

        if not await _can_watch_cohort(schema=schema, user_id=user.pk, cohort_id=cohort_id):
            await self.close(code=CLOSE_FORBIDDEN)
            return

        self._cohort_id = cohort_id  # remembered so the heartbeat can re-check scope (R1-05)
        await self.accept(subprotocol=accepted_subprotocol(self.scope))
        await self.join_group(f"{schema}.cohort.{cohort_id}")
        await self.start_heartbeat()

    async def _still_authorized(self) -> bool:
        """R1-05: re-run the connect-time branch/role gate each heartbeat, so a teacher
        whose role or branch membership is revoked mid-session is dropped (close 4403),
        not left watching the cohort's live marks."""
        user = self._authed_user()
        schema = self._schema()
        if user is None or schema is None:
            return False
        return await _can_watch_cohort(schema=schema, user_id=user.pk, cohort_id=self._cohort_id)

    async def attendance_update(self, event: dict) -> None:
        """Relay a producer payload (group_send type ``attendance.update``)."""
        payload = {k: v for k, v in event.items() if k != "type"}
        await self.send_json({"type": "attendance.update", "payload": payload})
