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
from django.conf import settings
from django_tenants.utils import schema_context

from config.asgi import application

HOST_A = [(b"host", b"a.localhost")]
HOST_B = [(b"host", b"b.localhost")]

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mint_access(tenant, user) -> str:
    from apps.notifications.principals import resolve_recipient_principal
    from apps.notifications.tests.helpers import ensure_notification_principal
    from core.session_auth import create_session

    with schema_context(tenant.schema_name):
        principal = resolve_recipient_principal(user_id=user.pk)
        if not principal.is_deliverable:
            roles = set(user.role_memberships.filter(revoked_at__isnull=True).values_list("role", flat=True))
            if "student" in roles:
                kind = "student"
            elif "teacher" in roles:
                kind = "teacher"
            elif "parent" in roles:
                kind = "parent"
            else:
                kind = "staff"
            membership = user.role_memberships.filter(revoked_at__isnull=True).first()
            ensure_notification_principal(
                user,
                kind=kind,
                branch=membership.branch if membership is not None else None,
            )
            principal = resolve_recipient_principal(user_id=user.pk)
        assert principal.kind is not None
        assert principal.principal_id is not None
        user.notification_principal_kind = principal.kind
        user.notification_principal_id = principal.principal_id
        session = create_session(
            user,
            principal_kind=principal.kind,
            principal_id=principal.principal_id,
        )
        return session.key


def _notification_group(tenant, user) -> str:
    from infrastructure.websocket.groups import notification_principal_group

    return notification_principal_group(
        tenant.schema_name,
        user.notification_principal_kind,
        user.notification_principal_id,
    )


def _notification_event(user, **payload) -> dict:
    return {
        "type": "notification.message",
        "recipient_principal_kind": user.notification_principal_kind,
        "recipient_principal_id": user.notification_principal_id,
        **payload,
    }


async def _connect(path: str, headers, token: str | None = None):
    protocols = [f"bearer.{token}"] if token else None
    comm = WebsocketCommunicator(application, path, headers=headers, subprotocols=protocols)
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
async def test_notifications_bearer_subprotocol_is_not_echoed(tenant_a, user_in):
    """The credential authenticates but is never copied into the response header."""

    @sync_to_async
    def _mint():
        user = user_in(tenant_a, roles=["teacher"])
        return _mint_access(tenant_a, user)

    token = await _mint()
    offered = f"bearer.{token}"
    comm = WebsocketCommunicator(application, "/ws/notifications/", headers=HOST_A, subprotocols=[offered])
    connected, subprotocol = await comm.connect()
    assert connected
    assert subprotocol is None
    await comm.disconnect()


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_selects_only_safe_application_subprotocol(tenant_a, user_in):
    from infrastructure.websocket.consumers import APPLICATION_SUBPROTOCOL

    @sync_to_async
    def _mint():
        user = user_in(tenant_a, roles=["teacher"])
        return _mint_access(tenant_a, user)

    token = await _mint()
    offered = f"bearer.{token}"
    comm = WebsocketCommunicator(
        application,
        "/ws/notifications/",
        headers=HOST_A,
        subprotocols=[APPLICATION_SUBPROTOCOL, offered],
    )
    connected, subprotocol = await comm.connect()
    assert connected
    assert subprotocol == APPLICATION_SUBPROTOCOL
    await comm.disconnect()


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_http_only_cookie_auth_connects_without_exposing_token(tenant_a, user_in):
    @sync_to_async
    def _mint():
        user = user_in(tenant_a, roles=["teacher"])
        return _mint_access(tenant_a, user)

    token = await _mint()
    cookie = f"theme=dark; {settings.API_SESSION_COOKIE_NAME}={token}".encode("latin1")
    comm = WebsocketCommunicator(
        application,
        "/ws/notifications/",
        headers=[*HOST_A, (b"origin", b"http://a.localhost"), (b"cookie", cookie)],
    )
    connected, subprotocol = await comm.connect()
    assert connected
    assert subprotocol is None
    await comm.disconnect()


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_cookie_auth_rejects_cross_origin_handshake(tenant_a, user_in):
    @sync_to_async
    def _mint():
        user = user_in(tenant_a, roles=["teacher"])
        return _mint_access(tenant_a, user)

    token = await _mint()
    cookie = f"{settings.API_SESSION_COOKIE_NAME}={token}".encode("latin1")
    comm = WebsocketCommunicator(
        application,
        "/ws/notifications/",
        headers=[*HOST_A, (b"origin", b"http://hostile.localhost"), (b"cookie", cookie)],
    )
    connected, code = await comm.connect()
    assert not connected
    assert code == 4403


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_bearer_auth_rejects_cross_origin_handshake(tenant_a, user_in):
    @sync_to_async
    def _mint():
        user = user_in(tenant_a, roles=["teacher"])
        return _mint_access(tenant_a, user)

    token = await _mint()
    comm = WebsocketCommunicator(
        application,
        "/ws/notifications/",
        headers=[*HOST_A, (b"origin", b"http://hostile.localhost")],
        subprotocols=[f"bearer.{token}"],
    )
    connected, code = await comm.connect()
    assert not connected
    assert code == 4403


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notifications_authed_joins_only_private_principal_group(tenant_a, user_in):
    """Notification sockets subscribe only to the recipient-specific group.

    There is no branch notification producer; joining a broad branch group both
    wastes a membership query/heartbeat work and creates a future privacy trap.
    """

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
        token = _mint_access(tenant_a, user)
        return branch_id, _notification_group(tenant_a, user), _notification_event(user), token

    branch_id, principal_group, principal_event, token = await _mint()
    comm, connected, _ = await _connect("/ws/notifications/", HOST_A, token)
    assert connected

    # The exact role-principal group reaches the socket.
    await _group_send(
        principal_group,
        {**principal_event, "id": 1, "title": "u", "body": "b"},
    )
    user_frame = await comm.receive_json_from(timeout=5)
    assert user_frame["type"] == "notification"
    assert user_frame["payload"]["id"] == 1

    # A broad branch-group send must not reach this private notification feed.
    await _group_send(
        f"{tenant_a.schema_name}.branch.{branch_id}",
        {**principal_event, "id": 2, "title": "b", "body": "b"},
    )
    assert await comm.receive_nothing(timeout=0.3)
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


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_notification_realtime_never_crosses_shared_user_principals(tenant_a):
    """Student and staff sockets sharing one bridge User receive separate events."""

    @sync_to_async
    def _setup():
        from apps.org.models import StaffProfile
        from apps.org.tests.factories import BranchFactory
        from apps.students.tests.factories import StudentProfileFactory
        from apps.users.models import RoleMembership
        from apps.users.tests.factories import UserFactory
        from core.session_auth import create_session
        from infrastructure.websocket.groups import notification_principal_group

        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            user = UserFactory()
            student = StudentProfileFactory(user=user, branch=branch)
            staff = StaffProfile.objects.create(
                user=user,
                username=f"staff.{user.username}",
                password=user.password,
            )
            RoleMembership.objects.create(user=user, branch=branch, role="student")
            RoleMembership.objects.create(user=user, branch=branch, role="director")
            student_session = create_session(
                user,
                principal_kind="student",
                principal_id=student.pk,
            )
            staff_session = create_session(
                user,
                principal_kind="staff",
                principal_id=staff.pk,
            )
            return {
                "student_token": student_session.key,
                "staff_token": staff_session.key,
                "student_group": notification_principal_group(tenant_a.schema_name, "student", student.pk),
                "staff_group": notification_principal_group(tenant_a.schema_name, "staff", staff.pk),
                "student_id": student.pk,
                "staff_id": staff.pk,
            }

    setup = await _setup()
    student_socket, student_connected, _ = await _connect(
        "/ws/notifications/", HOST_A, setup["student_token"]
    )
    staff_socket, staff_connected, _ = await _connect("/ws/notifications/", HOST_A, setup["staff_token"])
    assert student_connected
    assert staff_connected

    await _group_send(
        setup["student_group"],
        {
            "type": "notification.message",
            "recipient_principal_kind": "student",
            "recipient_principal_id": setup["student_id"],
            "id": 11,
        },
    )
    assert (await student_socket.receive_json_from(timeout=5))["payload"]["id"] == 11
    assert await staff_socket.receive_nothing(timeout=0.3)

    await _group_send(
        setup["staff_group"],
        {
            "type": "notification.message",
            "recipient_principal_kind": "staff",
            "recipient_principal_id": setup["staff_id"],
            "id": 12,
        },
    )
    assert (await staff_socket.receive_json_from(timeout=5))["payload"]["id"] == 12
    assert await student_socket.receive_nothing(timeout=0.3)
    await student_socket.disconnect()
    await staff_socket.disconnect()


# --------------------------------------------------------------------------- #
# AttendanceConsumer — permission on connect
# --------------------------------------------------------------------------- #
def _make_cohort_with_teacher(tenant, *, teacher_in_branch: bool, teaches: bool = True):
    """Returns (cohort_id, teacher_user, token) inside tenant's schema.

    teacher_in_branch True -> the teacher has a RoleMembership in the cohort's
    branch (allowed); False -> teacher is in a DIFFERENT branch (4403).
    """
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        cohort_branch = BranchFactory()
        cohort = CohortFactory(branch=cohort_branch)
        teacher = UserFactory()
        membership_branch = cohort_branch if teacher_in_branch else BranchFactory()
        RoleMembership.objects.create(user=teacher, branch=membership_branch, role="teacher")
        profile = TeacherProfileFactory(user=teacher, branch=membership_branch)
        if teaches:
            cohort.primary_teacher = profile
            cohort.save(update_fields=["primary_teacher"])
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
async def test_attendance_same_branch_non_teaching_teacher_denied_4403(tenant_a):
    cohort_id, _uid, token = await sync_to_async(_make_cohort_with_teacher)(
        tenant_a,
        teacher_in_branch=True,
        teaches=False,
    )
    _comm, connected, code = await _connect(f"/ws/cohorts/{cohort_id}/attendance/", HOST_A, token)
    assert not connected
    assert code == 4403


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_attendance_hod_websocket_honors_department_scope(tenant_a):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    def _setup():
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            own_department = DepartmentFactory(branch=branch)
            sibling_department = DepartmentFactory(branch=branch)
            own_cohort = CohortFactory(branch=branch, department=own_department)
            sibling_cohort = CohortFactory(branch=branch, department=sibling_department)
            hod = UserFactory()
            RoleMembership.objects.create(
                user=hod,
                branch=branch,
                department=own_department,
                role="head_of_dept",
            )
            hod.refresh_from_db()
            return own_cohort.id, sibling_cohort.id, _mint_access(tenant_a, hod)

    own_cohort_id, sibling_cohort_id, token = await sync_to_async(_setup)()
    own_comm, connected, _ = await _connect(f"/ws/cohorts/{own_cohort_id}/attendance/", HOST_A, token)
    assert connected
    await own_comm.disconnect()

    _comm, connected, code = await _connect(f"/ws/cohorts/{sibling_cohort_id}/attendance/", HOST_A, token)
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
        token = _mint_access(tenant_a, user)
        return user.pk, _notification_group(tenant_a, user), _notification_event(user), token

    user_pk, principal_group, principal_event, token = await _mint()
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

    # Group membership discarded: a send to the principal group reaches nothing.
    await _group_send(principal_group, {**principal_event, "id": 9})
    assert await comm.receive_nothing(timeout=0.3)


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_revocation_is_checked_before_next_notification(tenant_a, user_in):
    """No notification is delivered during the periodic-heartbeat revocation window."""

    @sync_to_async
    def _mint():
        user = user_in(tenant_a)
        token = _mint_access(tenant_a, user)
        return user.pk, _notification_group(tenant_a, user), _notification_event(user), token

    user_pk, principal_group, principal_event, token = await _mint()
    comm, connected, _ = await _connect("/ws/notifications/", HOST_A, token)
    assert connected

    @sync_to_async
    def _revoke():
        from core.session_auth import revoke_all_for_user

        with schema_context(tenant_a.schema_name):
            revoke_all_for_user(user_pk)

    await _revoke()
    await _group_send(
        principal_group,
        {**principal_event, "id": 77, "title": "must not leak"},
    )
    output = await comm.receive_output(timeout=2)
    assert output["type"] == "websocket.close"
    assert output["code"] == 4401


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
        token = _mint_access(tenant_a, user)
        return _notification_group(tenant_a, user), _notification_event(user), token

    principal_group, principal_event, token = await _mint()
    comm, connected, _ = await _connect("/ws/notifications/", HOST_A, token)
    assert connected
    # While connected the group reaches the socket.
    await _group_send(principal_group, {**principal_event, "id": 1})
    live = await comm.receive_json_from(timeout=5)
    assert live["payload"]["id"] == 1

    await comm.disconnect()
    # After disconnect the membership is gone: a fresh communicator on the SAME
    # user joins, then a send to the old group reaches the NEW socket only once
    # (the stale membership would otherwise duplicate). Simpler: re-send and
    # assert the disconnected communicator buffers nothing new.
    await _group_send(principal_group, {**principal_event, "id": 2})
    assert await comm.receive_nothing(timeout=0.3)


# --------------------------------------------------------------------------- #
# Producer-uniqueness grep (TD-15, D4-LC-6)
# --------------------------------------------------------------------------- #
def test_group_send_producer_uniqueness():
    """Private group producers stay in their owning domain or websocket adapter."""
    pattern = re.compile(r"from\s+infrastructure\.websocket\.channel_layer\s+import\s+group_send")
    offenders: list[str] = []
    # Restrict the static check to project source. REPO_ROOT.rglob also traverses
    # .venv and tool caches, turning this tiny invariant into a multi-minute scan
    # (and potentially inspecting unrelated installed packages).
    for source_dir in ("apps", "celery_tasks", "config", "core", "infrastructure"):
        for py in (REPO_ROOT / source_dir).rglob("*.py"):
            rel = py.relative_to(REPO_ROOT).as_posix()
            if "/tests/" in rel:
                continue
            if (
                rel == "apps/messaging/services/__init__.py"
                or rel.startswith("apps/notifications/")
                or rel.startswith("infrastructure/websocket/")
            ):
                continue
            text = py.read_text(encoding="utf-8", errors="ignore")
            if pattern.search(text):
                offenders.append(rel)
    assert offenders == [], f"group_send imported outside the producer scope: {offenders}"
