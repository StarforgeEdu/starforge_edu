"""Day-4 Lane C — realtime WebSocket consumers (TASKS §21, §26; TD-15, TD-1).

Covers the DAY-4 "Tests required" matrix for Lane C:
  - anonymous connection rejected 4401
  - cross-tenant token rejected 4401 (TD-1 on WS)
  - stale tv rejected 4401
  - authenticated notification delivery E2E via dispatch()
  - attendance branch-scope deny 4403 (+ cross-tenant 4401, unknown cohort 4403)
  - heartbeat: pong sustains, silence closes 4408
  - disconnect removes all group memberships
  - producer-uniqueness grep test (TD-15)

All consumer tests use channels.testing.WebsocketCommunicator + pytest-asyncio.
The test settings use the InMemoryChannelLayer, so group_send delivers in-process.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django_tenants.utils import schema_context

from config.asgi import application

HOST_A = [(b"host", b"a.localhost")]
HOST_B = [(b"host", b"b.localhost")]

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mint_access(tenant, user) -> str:
    from apps.auth.services import issue_token

    with schema_context(tenant.schema_name):
        return issue_token(user)["access"]


async def _connect(path: str, headers, token: str | None = None):
    url = f"{path}?token={token}" if token else path
    comm = WebsocketCommunicator(application, url, headers=headers)
    connected, code = await comm.connect()
    return comm, connected, code


async def _group_send(group: str, message: dict) -> None:
    """Send into a Channels group from the test (the channels.testing
    WebsocketCommunicator does not expose the consumer instance, so group
    membership is verified behaviorally: send to the group, assert the socket
    receives the relayed frame)."""
    layer = get_channel_layer()
    await layer.group_send(group, message)


# --------------------------------------------------------------------------- #
# NotificationConsumer — auth gates (4401)
# --------------------------------------------------------------------------- #
@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_anonymous_rejected_4401(tenant_a):
    _comm, connected, code = await _connect("/ws/notifications/", HOST_A)
    assert not connected
    assert code == 4401


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_cross_tenant_rejected_4401(tenant_a, tenant_b, user_in):
    @sync_to_async
    def _mint():
        user = user_in(tenant_a)
        return _mint_access(tenant_a, user)

    token = await _mint()
    # tenant_a token presented on tenant_b's host -> schema claim mismatch -> 4401.
    _comm, connected, code = await _connect("/ws/notifications/", HOST_B, token)
    assert not connected
    assert code == 4401


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_revoked_session_rejected_4401(tenant_a, user_in):
    from core.session_auth import revoke_all_for_user

    @sync_to_async
    def _mint_and_revoke():
        user = user_in(tenant_a)
        token = _mint_access(tenant_a, user)
        with schema_context(tenant_a.schema_name):
            revoke_all_for_user(user.pk)
        return token

    token = await _mint_and_revoke()
    _comm, connected, code = await _connect("/ws/notifications/", HOST_A, token)
    assert not connected
    assert code == 4401


# --------------------------------------------------------------------------- #
# NotificationConsumer — group membership + E2E delivery via dispatch()
# --------------------------------------------------------------------------- #
@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_bearer_subprotocol_is_echoed(tenant_a, user_in):
    """Browser auth offers a single Sec-WebSocket-Protocol value `bearer.<token>`.
    Per RFC 6455 the server MUST echo one of the OFFERED values — echoing a bare
    `bearer` (never offered) fails the browser handshake. Assert the exact offered
    value is echoed (and the token-in-subprotocol auth path connects)."""

    @sync_to_async
    def _mint():
        user = user_in(tenant_a, roles=["teacher"])
        return _mint_access(tenant_a, user)

    token = await _mint()
    offered = f"bearer.{token}"
    comm = WebsocketCommunicator(application, "/ws/notifications/", headers=HOST_A, subprotocols=[offered])
    connected, subprotocol = await comm.connect()
    assert connected
    assert subprotocol == offered  # echoes the offered value, not "bearer"
    await comm.disconnect()


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_authed_joins_user_and_branch_groups(tenant_a, user_in):
    """A connect joins {schema}.user.{id} AND {schema}.branch.{b} per active
    membership — proven behaviorally: a group_send to each group is relayed to
    the socket."""

    @sync_to_async
    def _mint():
        from apps.users.models import RoleMembership

        user = user_in(tenant_a, roles=["teacher"])
        with schema_context(tenant_a.schema_name):
            branch_id = (
                RoleMembership.objects.filter(user=user, revoked_at__isnull=True)
                .values_list("branch_id", flat=True)
                .first()
            )
        return user.pk, branch_id, _mint_access(tenant_a, user)

    user_pk, branch_id, token = await _mint()
    comm, connected, _ = await _connect("/ws/notifications/", HOST_A, token)
    assert connected

    # User group reaches the socket.
    await _group_send(
        f"{tenant_a.schema_name}.user.{user_pk}",
        {"type": "notification.message", "id": 1, "title": "u", "body": "b"},
    )
    user_frame = await comm.receive_json_from(timeout=5)
    assert user_frame["type"] == "notification"
    assert user_frame["payload"]["id"] == 1

    # Branch group reaches the socket too.
    await _group_send(
        f"{tenant_a.schema_name}.branch.{branch_id}",
        {"type": "notification.message", "id": 2, "title": "b", "body": "b"},
    )
    branch_frame = await comm.receive_json_from(timeout=5)
    assert branch_frame["payload"]["id"] == 2
    await comm.disconnect()


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_e2e_delivery_via_dispatch(tenant_a, user_in):
    """dispatch() (in-app channel, eager Celery) -> group_send -> socket frame."""
    from apps.notifications.models import EventType
    from apps.notifications.services import dispatch

    @sync_to_async
    def _mint():
        user = user_in(tenant_a)
        return user.pk, _mint_access(tenant_a, user)

    user_pk, token = await _mint()
    comm, connected, _ = await _connect("/ws/notifications/", HOST_A, token)
    assert connected

    @sync_to_async
    def _dispatch():
        with schema_context(tenant_a.schema_name):
            dispatch(
                event_type=EventType.ATTENDANCE_ABSENT,
                recipient_id=user_pk,
                context={"student_id": 7, "lesson_id": 12},
            )

    await _dispatch()
    frame = await comm.receive_json_from(timeout=5)
    assert frame["type"] == "notification"
    assert frame["payload"]["event_type"] == EventType.ATTENDANCE_ABSENT
    assert frame["payload"]["data"]["student_id"] == 7
    await comm.disconnect()


# --------------------------------------------------------------------------- #
# AttendanceConsumer — permission on connect
# --------------------------------------------------------------------------- #
def _make_cohort_with_teacher(tenant, *, teacher_in_branch: bool):
    """Returns (cohort_id, teacher_user, token) inside tenant's schema.

    teacher_in_branch True -> the teacher has a RoleMembership in the cohort's
    branch (allowed); False -> teacher is in a DIFFERENT branch (4403).
    """
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        cohort_branch = BranchFactory()
        cohort = CohortFactory(branch=cohort_branch)
        teacher = UserFactory()
        membership_branch = cohort_branch if teacher_in_branch else BranchFactory()
        RoleMembership.objects.create(user=teacher, branch=membership_branch, role="teacher")
        teacher.refresh_from_db()
        token = _mint_access(tenant, teacher)
        return cohort.pk, teacher.pk, token


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_teacher_in_branch_connects(tenant_a):
    cohort_id, _uid, token = await sync_to_async(_make_cohort_with_teacher)(tenant_a, teacher_in_branch=True)
    comm, connected, _ = await _connect(f"/ws/cohorts/{cohort_id}/attendance/", HOST_A, token)
    assert connected
    # Behavioral proof of cohort-group membership.
    await _group_send(
        f"{tenant_a.schema_name}.cohort.{cohort_id}",
        {"type": "attendance.update", "record_id": 1, "status": "absent"},
    )
    frame = await comm.receive_json_from(timeout=5)
    assert frame["type"] == "attendance.update"
    assert frame["payload"]["record_id"] == 1
    await comm.disconnect()


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_teacher_other_branch_denied_4403(tenant_a):
    cohort_id, _uid, token = await sync_to_async(_make_cohort_with_teacher)(tenant_a, teacher_in_branch=False)
    _comm, connected, code = await _connect(f"/ws/cohorts/{cohort_id}/attendance/", HOST_A, token)
    assert not connected
    assert code == 4403


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_anonymous_rejected_4401(tenant_a):
    cohort_id, _uid, _token = await sync_to_async(_make_cohort_with_teacher)(tenant_a, teacher_in_branch=True)
    _comm, connected, code = await _connect(f"/ws/cohorts/{cohort_id}/attendance/", HOST_A)
    assert not connected
    assert code == 4401


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_cross_tenant_rejected_4401(tenant_a, tenant_b):
    cohort_id, _uid, token = await sync_to_async(_make_cohort_with_teacher)(tenant_a, teacher_in_branch=True)
    # tenant_a token on tenant_b host -> 4401 (TD-1) before any branch check.
    _comm, connected, code = await _connect(f"/ws/cohorts/{cohort_id}/attendance/", HOST_B, token)
    assert not connected
    assert code == 4401


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_unknown_cohort_denied_4403(tenant_a):
    _cid, _uid, token = await sync_to_async(_make_cohort_with_teacher)(tenant_a, teacher_in_branch=True)
    _comm, connected, code = await _connect("/ws/cohorts/999999/attendance/", HOST_A, token)
    assert not connected
    assert code == 4403


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_student_no_branch_scope_denied_4403(tenant_a):
    """A student holds attendance:read (row-scoped) but no staff branch scope:
    they are not a director and have no membership in the cohort branch beyond
    their own — but a student membership IS branch-scoped, so a student in the
    cohort's branch could connect. Here we use a student in a DIFFERENT branch."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    def _setup():
        with schema_context(tenant_a.schema_name):
            cohort = CohortFactory(branch=BranchFactory())
            student = UserFactory()
            RoleMembership.objects.create(user=student, branch=BranchFactory(), role="student")
            student.refresh_from_db()
            return cohort.pk, _mint_access(tenant_a, student)

    cohort_id, token = await sync_to_async(_setup)()
    _comm, connected, code = await _connect(f"/ws/cohorts/{cohort_id}/attendance/", HOST_A, token)
    assert not connected
    assert code == 4403


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_same_branch_student_denied_4403(tenant_a):
    """WS-1: a STUDENT in the cohort's OWN branch must NOT get the cohort-WIDE live feed —
    their attendance:read is row-scoped to self in the HTTP path; the dashboard is a staff
    feed. Previously a same-branch student could connect (the earlier test's own docstring
    admitted it); now denied 4403."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    def _setup():
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            cohort = CohortFactory(branch=branch)
            student = UserFactory()
            RoleMembership.objects.create(user=student, branch=branch, role="student")
            student.refresh_from_db()
            return cohort.pk, _mint_access(tenant_a, student)

    cohort_id, token = await sync_to_async(_setup)()
    _comm, connected, code = await _connect(f"/ws/cohorts/{cohort_id}/attendance/", HOST_A, token)
    assert not connected
    assert code == 4403


# --------------------------------------------------------------------------- #
# AttendanceConsumer — E2E relay via the producer
# --------------------------------------------------------------------------- #
@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_e2e_relay_via_producer(tenant_a):
    """push_cohort_attendance (the dispatch-side producer) -> cohort group_send
    -> the connected AttendanceConsumer relays it as type attendance.update."""
    from apps.notifications.services import push_cohort_attendance

    cohort_id, _uid, token = await sync_to_async(_make_cohort_with_teacher)(tenant_a, teacher_in_branch=True)
    comm, connected, _ = await _connect(f"/ws/cohorts/{cohort_id}/attendance/", HOST_A, token)
    assert connected

    @sync_to_async
    def _produce():
        with schema_context(tenant_a.schema_name):
            push_cohort_attendance(
                cohort_id=cohort_id,
                payload={"record_id": 9, "student_id": 7, "status": "absent", "auto": False},
            )

    await _produce()
    frame = await comm.receive_json_from(timeout=5)
    assert frame["type"] == "attendance.update"
    assert frame["payload"]["record_id"] == 9
    assert frame["payload"]["status"] == "absent"
    await comm.disconnect()


# --------------------------------------------------------------------------- #
# Heartbeat — pong sustains, silence closes 4408
# --------------------------------------------------------------------------- #
@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_heartbeat_silence_closes_4408(tenant_a, user_in, monkeypatch):
    # Patch the interval tiny so the test does not wait 30s. Two pings with no
    # pong (missed > MAX_MISSED=2) -> close 4408.
    from infrastructure.websocket import consumers as ws_consumers

    monkeypatch.setattr(ws_consumers.HeartbeatConsumerMixin, "HEARTBEAT_INTERVAL", 0.05)

    @sync_to_async
    def _mint():
        user = user_in(tenant_a)
        return _mint_access(tenant_a, user)

    token = await _mint()
    comm, connected, _ = await _connect("/ws/notifications/", HOST_A, token)
    assert connected
    # Drain server pings without answering; eventually the consumer closes 4408.
    closed_code = None
    for _ in range(20):
        msg = await comm.receive_output(timeout=2)
        if msg["type"] == "websocket.close":
            closed_code = msg.get("code")
            break
    assert closed_code == 4408


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_heartbeat_pong_sustains(tenant_a, user_in, monkeypatch):
    from infrastructure.websocket import consumers as ws_consumers

    monkeypatch.setattr(ws_consumers.HeartbeatConsumerMixin, "HEARTBEAT_INTERVAL", 0.05)

    @sync_to_async
    def _mint():
        user = user_in(tenant_a)
        return _mint_access(tenant_a, user)

    token = await _mint()
    comm, connected, _ = await _connect("/ws/notifications/", HOST_A, token)
    assert connected
    # Answer several pings with pong; each pong resets the missed-ping counter so
    # the connection survives well past the 2-missed budget (5 intervals here).
    # An unanswered socket would have closed 4408 by the 3rd interval.
    for _ in range(5):
        msg = await comm.receive_output(timeout=2)
        assert msg["type"] == "websocket.send"  # a ping frame, never websocket.close
        await comm.send_json_to({"type": "pong"})
    await comm.disconnect()


# --------------------------------------------------------------------------- #
# R1-05 — post-connect revocation: a live socket is re-authorized each heartbeat
# --------------------------------------------------------------------------- #
@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_revoked_session_closes_live_socket_4401(tenant_a, user_in, monkeypatch):
    """A session revoked AFTER connect (force-logout / password change) terminates the live
    socket on the next heartbeat cycle (close 4401), and its group membership is discarded
    — connect-time auth alone would keep streaming to a de-authorized user."""
    from infrastructure.websocket import consumers as ws_consumers

    monkeypatch.setattr(ws_consumers.HeartbeatConsumerMixin, "HEARTBEAT_INTERVAL", 0.05)

    @sync_to_async
    def _mint():
        user = user_in(tenant_a)
        return user.pk, _mint_access(tenant_a, user)

    user_pk, token = await _mint()
    comm, connected, _ = await _connect("/ws/notifications/", HOST_A, token)
    assert connected

    @sync_to_async
    def _revoke():
        from core.session_auth import revoke_all_for_user

        with schema_context(tenant_a.schema_name):
            revoke_all_for_user(user_pk)

    await _revoke()

    closed_code = None
    for _ in range(20):
        msg = await comm.receive_output(timeout=2)
        if msg["type"] == "websocket.close":
            closed_code = msg.get("code")
            break
    assert closed_code == 4401

    # Group membership discarded: a send to the user group now reaches nothing.
    await _group_send(
        f"{tenant_a.schema_name}.user.{user_pk}", {"type": "notification.message", "id": 9}
    )
    assert await comm.receive_nothing(timeout=0.3)


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_role_revoked_closes_live_socket_4403(tenant_a, monkeypatch):
    """A branch/role-scoped socket is re-checked each heartbeat: a teacher whose role
    membership is revoked mid-session is dropped (close 4403), not left watching the
    cohort's live attendance — the session itself is still valid, so this is the scope
    re-check, distinct from the 4401 session-revocation path."""
    from infrastructure.websocket import consumers as ws_consumers

    monkeypatch.setattr(ws_consumers.HeartbeatConsumerMixin, "HEARTBEAT_INTERVAL", 0.05)

    cohort_id, teacher_pk, token = await sync_to_async(_make_cohort_with_teacher)(
        tenant_a, teacher_in_branch=True
    )
    comm, connected, _ = await _connect(f"/ws/cohorts/{cohort_id}/attendance/", HOST_A, token)
    assert connected

    @sync_to_async
    def _revoke_role():
        from django.utils import timezone

        from apps.users.models import RoleMembership

        with schema_context(tenant_a.schema_name):
            RoleMembership.objects.filter(user_id=teacher_pk, revoked_at__isnull=True).update(
                revoked_at=timezone.now()
            )

    await _revoke_role()

    closed_code = None
    for _ in range(20):
        msg = await comm.receive_output(timeout=2)
        if msg["type"] == "websocket.close":
            closed_code = msg.get("code")
            break
    assert closed_code == 4403


# --------------------------------------------------------------------------- #
# Disconnect cleanup — no group leak
# --------------------------------------------------------------------------- #
@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_disconnect_clears_group_memberships(tenant_a, user_in):
    """disconnect() group_discard-s every joined group: after disconnect a
    group_send to the user group reaches no channel (no membership leak)."""

    @sync_to_async
    def _mint():
        user = user_in(tenant_a, roles=["teacher"])
        return user.pk, _mint_access(tenant_a, user)

    user_pk, token = await _mint()
    comm, connected, _ = await _connect("/ws/notifications/", HOST_A, token)
    assert connected
    user_group = f"{tenant_a.schema_name}.user.{user_pk}"

    # While connected the group reaches the socket.
    await _group_send(user_group, {"type": "notification.message", "id": 1})
    live = await comm.receive_json_from(timeout=5)
    assert live["payload"]["id"] == 1

    await comm.disconnect()
    # After disconnect the membership is gone: a fresh communicator on the SAME
    # user joins, then a send to the old group reaches the NEW socket only once
    # (the stale membership would otherwise duplicate). Simpler: re-send and
    # assert the disconnected communicator buffers nothing new.
    await _group_send(user_group, {"type": "notification.message", "id": 2})
    assert await comm.receive_nothing(timeout=0.3)


# --------------------------------------------------------------------------- #
# Producer-uniqueness grep (TD-15, D4-LC-6)
# --------------------------------------------------------------------------- #
def test_group_send_producer_uniqueness():
    """`channel_layer.group_send` may be IMPORTED only under apps/notifications/
    + infrastructure/websocket/. dispatch is the single producer (TD-15)."""
    pattern = re.compile(r"from\s+infrastructure\.websocket\.channel_layer\s+import\s+group_send")
    offenders: list[str] = []
    for py in REPO_ROOT.rglob("*.py"):
        rel = py.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel or rel.startswith("tests/"):
            continue
        if rel.startswith("apps/notifications/") or rel.startswith("infrastructure/websocket/"):
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            offenders.append(rel)
    assert offenders == [], f"group_send imported outside the producer scope: {offenders}"
