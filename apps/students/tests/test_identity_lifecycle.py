"""Destructive student-account lifecycle and history-integrity regressions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import DatabaseError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_delete_deactivates_and_revokes_access_without_erasing_history(tenant_a, as_role):
    from apps.audit.models import AuditLog
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import Guardian, PickupAuthorization
    from apps.parents.services import create_parent, link_guardian
    from apps.students.models import EnrollmentEvent, StudentProfile
    from apps.students.services import create_student
    from apps.users.models import Device, RoleMembership, Session

    client, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = create_student(
            branch=branch,
            phone="+998905570001",
            status=StudentProfile.Status.ACTIVE,
        )
        parent = create_parent(phone="+998905570002")
        guardian = link_guardian(parent=parent, student=student, relationship="mother")
        pickup = PickupAuthorization.objects.create(
            student=student,
            full_name="Trusted relative",
            phone="+998905570003",
        )
        event_ids = set(student.enrollment_events.values_list("pk", flat=True))
        session = Session.objects.create(
            user=student.user,
            key_hash="test-student-lifecycle-session",
            expires_at=timezone.now() + timedelta(hours=1),
            principal_kind="student",
            principal_id=student.pk,
        )
        device = Device.objects.create(
            user=student.user,
            device_id="student-lifecycle-device",
            platform=Device.PLATFORM_WEB,
        )

    response = client.delete(f"/api/v1/students/{student.pk}/")
    assert response.status_code == 204, response.content

    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        student.user.refresh_from_db()
        session.refresh_from_db()
        device.refresh_from_db()
        assert student.is_active is False
        assert student.has_usable_password() is False
        assert student.user.is_active is False
        assert session.revoked_at is not None
        assert device.revoked_at is not None
        assert device.push_token == ""
        assert not RoleMembership.objects.filter(
            user=student.user,
            revoked_at__isnull=True,
        ).exists()
        assert Guardian.objects.filter(pk=guardian.pk, revoked_at__isnull=True).exists()
        assert PickupAuthorization.objects.filter(pk=pickup.pk, is_active=True).exists()
        assert event_ids.issubset(set(EnrollmentEvent.objects.values_list("pk", flat=True)))
        assert AuditLog.objects.filter(
            resource_type="students.StudentProfile",
            resource_id=str(student.pk),
            after__is_active=False,
        ).exists()

    detail = client.get(f"/api/v1/students/{student.pk}/")
    assert detail.status_code == 200
    assert detail.json()["data"]["is_active"] is False


def test_inactive_or_ambiguous_student_cannot_receive_credentials_or_be_deactivated_collaterally(
    tenant_a,
    as_role,
):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.tests.factories import ParentProfileFactory
    from apps.students.services import create_student
    from apps.users.models import RoleMembership

    client, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        inactive = create_student(branch=branch, phone="+998905570010")
        ambiguous = create_student(branch=branch, phone="+998905570011")
        ParentProfileFactory(user=ambiguous.user)
        ambiguous_membership = RoleMembership.objects.get(
            user=ambiguous.user,
            revoked_at__isnull=True,
        )

    assert client.delete(f"/api/v1/students/{inactive.pk}/").status_code == 204
    credentials = client.post(f"/api/v1/students/{inactive.pk}/credentials/", {}, format="json")
    assert credentials.status_code == 409
    assert credentials.json()["code"] == "account_inactive"

    denied = client.delete(f"/api/v1/students/{ambiguous.pk}/")
    assert denied.status_code == 409
    assert denied.json()["code"] == "identity_bridge_ambiguous"
    with schema_context(tenant_a.schema_name):
        ambiguous.refresh_from_db()
        ambiguous.user.refresh_from_db()
        ambiguous_membership.refresh_from_db()
        assert ambiguous.is_active is True
        assert ambiguous.user.is_active is True
        assert ambiguous_membership.revoked_at is None


def test_student_mutations_reject_unknown_or_lifecycle_bypass_fields(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory
    from apps.students.services import create_student

    client, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = create_student(branch=BranchFactory(), phone="+998905570020")

    for payload in ({"is_active": False}, {"branch": 999}, {"unexpected": "value"}):
        response = client.patch(f"/api/v1/students/{student.pk}/", payload, format="json")
        assert response.status_code == 400
        assert set(response.json()["errors"]) == set(payload)
    transition = client.post(
        f"/api/v1/students/{student.pk}/transition/",
        {"to_status": "application", "silent": True},
        format="json",
    )
    assert transition.status_code == 400
    assert set(transition.json()["errors"]) == {"silent"}


def test_student_update_cannot_remove_every_recovery_contact(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory
    from apps.students.services import create_student

    client, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = create_student(branch=BranchFactory(), phone="+998905570025")

    response = client.patch(
        f"/api/v1/students/{student.pk}/",
        {"phone": "", "email": ""},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["code"] == "identifier_required"
    assert set(response.json()["errors"]) == {"phone", "email"}
    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        assert student.phone == "+998905570025"


@pytest.mark.django_db(transaction=True)
def test_database_rejects_direct_student_and_enrollment_history_deletion(tenant_a):
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import EnrollmentEvent, StudentProfile
    from apps.students.services import create_student

    with schema_context(tenant_a.schema_name):
        student = create_student(
            branch=BranchFactory(),
            phone="+998905570030",
            status=StudentProfile.Status.ACTIVE,
        )
        event = student.enrollment_events.first()
        assert event is not None
        with pytest.raises(DatabaseError), transaction.atomic():
            StudentProfile.objects.filter(pk=student.pk).delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            EnrollmentEvent.objects.filter(pk=event.pk).delete()
