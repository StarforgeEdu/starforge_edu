"""A role-native session cannot borrow grants or notifications across one bridge User."""

from __future__ import annotations

import uuid

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django_tenants.utils import schema_context

from config.asgi import application

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "Principal-Isolation-42"


def _multi_principal_accounts(tenant):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.notifications.models import EventType, Notification
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        suffix = uuid.uuid4().hex[:12]
        branch = BranchFactory()
        user = UserFactory()
        student_username = f"isolated.student.{suffix}"
        teacher_username = f"isolated.teacher.{suffix}"
        staff_username = f"isolated.staff.{suffix}"
        student = StudentProfileFactory(
            user=user,
            branch=branch,
            username=student_username,
            phone="",
            email="",
        )
        teacher = TeacherProfileFactory(
            user=user,
            branch=branch,
            username=teacher_username,
            phone="",
            email="",
        )
        staff = StaffProfile.objects.create(user=user, username=staff_username)
        for account in (student, teacher, staff):
            account.set_password(PASSWORD)
            account.save(update_fields=["password"])
        RoleMembership.objects.create(user=user, branch=branch, role="student")
        RoleMembership.objects.create(user=user, branch=branch, role="teacher")
        RoleMembership.objects.create(user=user, branch=branch, role="director")
        cohort = CohortFactory(branch=branch)
        staff_notification = Notification.objects.create(
            user=user,
            event_type=EventType.REPORT_READY,
            title="Staff-only report",
            recipient_principal_kind="staff",
            recipient_principal_id=staff.pk,
        )
        student_notification = Notification.objects.create(
            user=user,
            event_type=EventType.REPORT_READY,
            title="Student-only report",
            recipient_principal_kind="student",
            recipient_principal_id=student.pk,
        )
        return {
            "user_id": user.pk,
            "cohort_id": cohort.pk,
            "staff_notification_id": staff_notification.pk,
            "student_notification_id": student_notification.pk,
            "student_id": student.pk,
            "staff_id": staff.pk,
            "student_username": student_username,
            "teacher_username": teacher_username,
            "staff_username": staff_username,
        }


def _role_access(client, username: str) -> str:
    response = client.post(
        "/api/v1/auth/role-login/",
        {"username": username, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.content
    return response.json()["data"]["access"]


def test_role_session_permissions_are_principal_kind_scoped(tenant_a, client_for):
    accounts = _multi_principal_accounts(tenant_a)
    login = client_for(tenant_a)

    student_access = _role_access(login, accounts["student_username"])
    student = client_for(tenant_a)
    student.credentials(HTTP_AUTHORIZATION=f"Bearer {student_access}")
    student_me = student.get("/api/v1/users/me/").json()["data"]
    assert {row["account_kind"] for row in student_me["role_memberships"]} == {"student"}
    assert "access:read" not in student_me["effective_permissions"]
    denied = student.get("/api/v1/access/types/")
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"

    teacher_access = _role_access(login, accounts["teacher_username"])
    teacher = client_for(tenant_a)
    teacher.credentials(HTTP_AUTHORIZATION=f"Bearer {teacher_access}")
    assert teacher.get("/api/v1/access/types/").status_code == 403

    staff_access = _role_access(login, accounts["staff_username"])
    staff = client_for(tenant_a)
    staff.credentials(HTTP_AUTHORIZATION=f"Bearer {staff_access}")
    staff_me = staff.get("/api/v1/users/me/").json()["data"]
    assert {row["account_kind"] for row in staff_me["role_memberships"]} == {"staff"}
    assert staff.get("/api/v1/access/types/").status_code == 200


def test_inactive_bridge_user_has_no_background_authorization_context(tenant_a):
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory
    from core.permissions import (
        get_unambiguous_user_authorization_context,
        get_user_authorization_context,
    )

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        user = UserFactory()
        staff = StaffProfile.objects.create(user=user, username=f"inactive.{uuid.uuid4().hex[:12]}")
        RoleMembership.objects.create(user=user, branch=branch, role="director")
        type(user).objects.filter(pk=user.pk).update(is_active=False)
        user.is_active = False

        roles, memberships = get_user_authorization_context(
            user,
            principal_kind="staff",
            principal_id=staff.pk,
            # Even a previously validated task snapshot must lose authority as
            # soon as the bridge account is deactivated.
            principal_validated=True,
        )
        assert not roles
        assert memberships == []
        legacy_roles, legacy_memberships = get_unambiguous_user_authorization_context(user)
        assert not legacy_roles
        assert legacy_memberships == []


def test_role_session_notification_http_feed_is_exactly_principal_scoped(tenant_a, client_for):
    accounts = _multi_principal_accounts(tenant_a)
    access = _role_access(client_for(tenant_a), accounts["student_username"])
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    response = client.get("/api/v1/notifications/")
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["results"]}
    assert ids == {accounts["student_notification_id"]}
    assert "Staff-only report" not in response.content.decode()

    # Read receipts use the same exact principal boundary. Sharing the bridge
    # User must not make another role's row mutable by identifier.
    hidden = client.post(f"/api/v1/notifications/{accounts['staff_notification_id']}/read/")
    assert hidden.status_code == 404
    assert client.post("/api/v1/notifications/read-all/").status_code == 200

    staff_access = _role_access(client_for(tenant_a), accounts["staff_username"])
    staff = client_for(tenant_a)
    staff.credentials(HTTP_AUTHORIZATION=f"Bearer {staff_access}")
    staff_ids = {row["id"] for row in staff.get("/api/v1/notifications/").json()["results"]}
    assert staff_ids == {accounts["staff_notification_id"]}
    assert staff.get("/api/v1/notifications/unread-count/").json()["data"]["count"] == 1


@pytest.mark.channels
@pytest.mark.asyncio
async def test_multi_profile_notification_websocket_uses_session_principal(tenant_a, client_for):
    from channels.layers import get_channel_layer

    from infrastructure.websocket.groups import notification_principal_group

    accounts = await sync_to_async(_multi_principal_accounts)(tenant_a)

    def _login():
        return _role_access(client_for(tenant_a), accounts["student_username"])

    access = await sync_to_async(_login)()
    communicator = WebsocketCommunicator(
        application,
        "/ws/notifications/",
        headers=[(b"host", b"a.localhost")],
        subprotocols=[f"bearer.{access}"],
    )
    connected, code = await communicator.connect()
    assert connected is True
    assert code is None

    layer = get_channel_layer()
    assert layer is not None
    student_group = notification_principal_group(
        tenant_a.schema_name,
        "student",
        accounts["student_id"],
    )
    staff_group = notification_principal_group(
        tenant_a.schema_name,
        "staff",
        accounts["staff_id"],
    )
    event = {
        "type": "notification.message",
        "id": 123,
        "event_type": "report.ready",
        "title": "private",
        "body": "",
        "data": {},
        "created_at": "2026-08-02T00:00:00+00:00",
    }
    await layer.group_send(
        staff_group,
        {
            **event,
            "recipient_principal_kind": "staff",
            "recipient_principal_id": accounts["staff_id"],
        },
    )
    assert await communicator.receive_nothing(timeout=0.05)

    # Defense in depth: even a producer bug that publishes wrong recipient
    # metadata to the correct group is rejected before it reaches the socket.
    await layer.group_send(
        student_group,
        {
            **event,
            "recipient_principal_kind": "staff",
            "recipient_principal_id": accounts["staff_id"],
        },
    )
    assert await communicator.receive_nothing(timeout=0.05)
    await layer.group_send(
        student_group,
        {
            **event,
            "recipient_principal_kind": "student",
            "recipient_principal_id": accounts["student_id"],
        },
    )
    assert await communicator.receive_json_from(timeout=1) == {
        "type": "notification",
        "payload": {
            "id": 123,
            "event_type": "report.ready",
            "title": "private",
            "body": "",
            "data": {},
            "created_at": "2026-08-02T00:00:00+00:00",
        },
    }
    await communicator.disconnect()


@pytest.mark.channels
@pytest.mark.asyncio
async def test_student_websocket_cannot_borrow_staff_attendance_scope(tenant_a, client_for):
    accounts = await sync_to_async(_multi_principal_accounts)(tenant_a)

    def _login():
        return _role_access(client_for(tenant_a), accounts["student_username"])

    access = await sync_to_async(_login)()
    communicator = WebsocketCommunicator(
        application,
        f"/ws/cohorts/{accounts['cohort_id']}/attendance/",
        headers=[(b"host", b"a.localhost")],
        subprotocols=[f"bearer.{access}"],
    )
    connected, code = await communicator.connect()
    assert connected is False
    assert code == 4403
    await communicator.wait()
