"""Role-native ownership regressions for persisted private messaging state."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.db import DatabaseError, IntegrityError, connection, transaction
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

THREADS = "/api/v1/messaging/threads/"


def _exact_client(client_for, tenant, *, user, kind: str, principal_id: int):
    from core.session_auth import create_session

    with schema_context(tenant.schema_name):
        session = create_session(
            user,
            principal_kind=kind,
            principal_id=principal_id,
        )
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
    return client


def _ids(response) -> set[int]:
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["data"]}


def test_same_bridge_role_sessions_own_separate_threads_and_delivery(tenant_a, client_for):
    """Student and staff sessions sharing one User never borrow private state."""
    from apps.messaging.models import (
        ParticipantAttributionStatus,
        Thread,
        ThreadParticipant,
    )
    from apps.notifications.models import Notification
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        shared_user = UserFactory()
        peer_user = UserFactory()
        student = StudentProfileFactory(user=shared_user, branch=branch)
        staff = StaffProfile.objects.create(user=shared_user, username="shared.messaging.staff")
        peer = StaffProfile.objects.create(user=peer_user, username="messaging.peer.staff")
        RoleMembership.objects.create(user=shared_user, branch=branch, role=Role.STUDENT)
        RoleMembership.objects.create(user=shared_user, branch=branch, role=Role.DIRECTOR)
        RoleMembership.objects.create(user=peer_user, branch=branch, role=Role.REGISTRAR)

        student_thread = Thread.objects.create(subject="Student account", branch=branch)
        staff_thread = Thread.objects.create(subject="Staff account", branch=branch)
        student_seat = ThreadParticipant.objects.create(
            thread=student_thread,
            user=shared_user,
            principal_kind="student",
            principal_id=student.pk,
            attribution_status=ParticipantAttributionStatus.CAPTURED,
        )
        staff_seat = ThreadParticipant.objects.create(
            thread=staff_thread,
            user=shared_user,
            principal_kind="staff",
            principal_id=staff.pk,
            attribution_status=ParticipantAttributionStatus.CAPTURED,
        )
        for thread in (student_thread, staff_thread):
            ThreadParticipant.objects.create(
                thread=thread,
                user=peer_user,
                principal_kind="staff",
                principal_id=peer.pk,
                attribution_status=ParticipantAttributionStatus.CAPTURED,
            )

    student_client = _exact_client(
        client_for,
        tenant_a,
        user=shared_user,
        kind="student",
        principal_id=student.pk,
    )
    staff_client = _exact_client(
        client_for,
        tenant_a,
        user=shared_user,
        kind="staff",
        principal_id=staff.pk,
    )
    peer_client = _exact_client(
        client_for,
        tenant_a,
        user=peer_user,
        kind="staff",
        principal_id=peer.pk,
    )

    assert _ids(student_client.get(THREADS)) == {student_thread.pk}
    assert _ids(staff_client.get(THREADS)) == {staff_thread.pk}
    assert student_client.get(f"{THREADS}{staff_thread.pk}/").status_code == 404
    assert staff_client.get(f"{THREADS}{student_thread.pk}/").status_code == 404

    # Detail gating runs before every mutation, so a sibling role cannot send,
    # mark read, or change the exact participant's preference.
    for url, method, payload in (
        (f"{THREADS}{staff_thread.pk}/messages/", "post", {"body": "borrow"}),
        (f"{THREADS}{staff_thread.pk}/read/", "post", {}),
        (
            f"{THREADS}{staff_thread.pk}/preferences/",
            "patch",
            {"notifications_muted": True},
        ),
    ):
        assert getattr(student_client, method)(url, payload, format="json").status_code == 404

    assert (
        student_client.patch(
            f"{THREADS}{student_thread.pk}/preferences/",
            {"notifications_muted": True},
            format="json",
        ).status_code
        == 200
    )
    assert student_client.post(f"{THREADS}{student_thread.pk}/read/", {}, format="json").status_code == 200
    assert (
        staff_client.post(
            f"{THREADS}{staff_thread.pk}/messages/",
            {"body": "staff-only"},
            format="json",
        ).status_code
        == 201
    )
    delivered = peer_client.post(
        f"{THREADS}{student_thread.pk}/messages/",
        {"body": "student-only"},
        format="json",
    )
    assert delivered.status_code == 201, delivered.content

    with schema_context(tenant_a.schema_name):
        student_seat.refresh_from_db()
        staff_seat.refresh_from_db()
        assert student_seat.notifications_muted is True
        assert student_seat.last_read_at is not None
        assert staff_seat.notifications_muted is False
        notifications = list(
            Notification.objects.filter(
                user=shared_user,
                event_type="message.received",
            ).values_list(
                "recipient_principal_kind",
                "recipient_principal_id",
                "attribution_status",
            )
        )
        # One came from the staff-only send's peer fanout only when applicable;
        # every row is nevertheless owned by exactly the intended role account.
        assert ("student", student.pk, "captured") in notifications
        assert all(
            (kind, principal_id) in {("student", student.pk), ("staff", staff.pk)}
            for kind, principal_id, _status in notifications
        )


def test_one_bridge_user_cannot_occupy_two_seats_in_one_thread(tenant_a):
    """The invariant keeps User-backed message sender/unread semantics exact."""
    from apps.messaging.models import ParticipantAttributionStatus, Thread, ThreadParticipant
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        user = UserFactory()
        branch = BranchFactory()
        student = StudentProfileFactory(user=user, branch=branch)
        staff = StaffProfile.objects.create(user=user, username="double.seat.staff")
        thread = Thread.objects.create(branch=branch)
        ThreadParticipant.objects.create(
            thread=thread,
            user=user,
            principal_kind="student",
            principal_id=student.pk,
            attribution_status=ParticipantAttributionStatus.CAPTURED,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            ThreadParticipant.objects.create(
                thread=thread,
                user=user,
                principal_kind="staff",
                principal_id=staff.pk,
                attribution_status=ParticipantAttributionStatus.CAPTURED,
            )


def test_participant_backfill_resolves_only_unique_live_profiles(tenant_a):
    from apps.messaging.models import Thread, ThreadParticipant
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.tests.factories import UserFactory

    migration = importlib.import_module(
        "apps.messaging.migrations.0006_threadparticipant_principal_attribution"
    )
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        unique_user = UserFactory()
        conflicting_user = UserFactory()
        missing_user = UserFactory()
        unique_profile = StudentProfileFactory(user=unique_user, branch=branch)
        StudentProfileFactory(user=conflicting_user, branch=branch)
        StaffProfile.objects.create(user=conflicting_user, username="conflicting.messaging.staff")
        rows = []
        for user in (unique_user, conflicting_user, missing_user):
            thread = Thread.objects.create(branch=branch)
            rows.append(ThreadParticipant(thread=thread, user=user))
        # Simulate the additive migration's pre-backfill state. bulk_create is
        # deliberate: ordinary save() already applies the same fail-closed policy.
        ThreadParticipant.objects.bulk_create(rows)
        migration.backfill_participant_principals(
            django_apps,
            SimpleNamespace(connection=connection),
        )
        for row in rows:
            row.refresh_from_db()
        assert (
            rows[0].principal_kind,
            rows[0].principal_id,
            rows[0].attribution_status,
        ) == ("student", unique_profile.pk, "resolved")
        assert (rows[1].principal_kind, rows[1].principal_id, rows[1].attribution_status) == (
            None,
            None,
            "conflicting",
        )
        assert (rows[2].principal_kind, rows[2].principal_id, rows[2].attribution_status) == (
            None,
            None,
            "unresolved",
        )


def test_participant_snapshot_is_immutable_and_live_owner_validated(tenant_a):
    from apps.messaging.models import ParticipantAttributionStatus, Thread, ThreadParticipant
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        owner = UserFactory()
        other = UserFactory()
        owner_profile = StudentProfileFactory(user=owner, branch=branch)
        other_profile = StudentProfileFactory(user=other, branch=branch)
        thread = Thread.objects.create(branch=branch)
        participant = ThreadParticipant.objects.create(
            thread=thread,
            user=owner,
            principal_kind="student",
            principal_id=owner_profile.pk,
            attribution_status=ParticipantAttributionStatus.CAPTURED,
        )

        with pytest.raises(DatabaseError), transaction.atomic():
            ThreadParticipant.objects.filter(pk=participant.pk).update(attribution_status="resolved")
        with pytest.raises(DatabaseError), transaction.atomic():
            ThreadParticipant.objects.bulk_create(
                [
                    ThreadParticipant(
                        thread=Thread.objects.create(branch=branch),
                        user=owner,
                        principal_kind="student",
                        principal_id=other_profile.pk,
                        attribution_status=ParticipantAttributionStatus.CAPTURED,
                    )
                ]
            )
