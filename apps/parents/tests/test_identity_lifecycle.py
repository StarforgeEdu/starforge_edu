"""Family identity, guardian, and pickup lifecycle regressions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import DatabaseError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_parent_delete_deactivates_account_and_retains_guardian_history(
    tenant_a,
    as_role,
):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import Guardian
    from apps.parents.services import create_parent, link_guardian
    from apps.students.services import create_student
    from apps.users.models import Device, RoleMembership, Session

    client, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = create_student(branch=BranchFactory(), phone="+998905571001")
        parent = create_parent(phone="+998905571002")
        guardian = link_guardian(
            parent=parent,
            student=student,
            relationship="legal_guardian",
            custody_notes="Retained encrypted court evidence",
        )
        session = Session.objects.create(
            user=parent.user,
            key_hash="test-parent-lifecycle-session",
            expires_at=timezone.now() + timedelta(hours=1),
            principal_kind="parent",
            principal_id=parent.pk,
        )
        device = Device.objects.create(
            user=parent.user,
            device_id="parent-lifecycle-device",
            platform=Device.PLATFORM_WEB,
        )

    response = client.delete(f"/api/v1/parents/{parent.pk}/")
    assert response.status_code == 204, response.content
    with schema_context(tenant_a.schema_name):
        parent.refresh_from_db()
        parent.user.refresh_from_db()
        session.refresh_from_db()
        device.refresh_from_db()
        retained = Guardian.objects.get(pk=guardian.pk)
        assert parent.is_active is False
        assert parent.user.is_active is False
        assert retained.custody_notes == "Retained encrypted court evidence"
        assert session.revoked_at is not None
        assert device.revoked_at is not None
        assert not RoleMembership.objects.filter(
            user=parent.user,
            revoked_at__isnull=True,
        ).exists()

    detail = client.get(f"/api/v1/parents/{parent.pk}/")
    assert detail.status_code == 200
    assert detail.json()["data"]["is_active"] is False
    credentials = client.post(f"/api/v1/parents/{parent.pk}/credentials/", {}, format="json")
    assert credentials.status_code == 409
    assert credentials.json()["code"] == "account_inactive"


def test_guardian_delete_revokes_history_and_removes_every_parent_read_path(
    tenant_a,
    as_role,
    as_user,
):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import Guardian
    from apps.parents.services import create_parent, link_guardian
    from apps.students.services import create_student
    from apps.users.models import RoleMembership

    director, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = create_student(branch=BranchFactory(), phone="+998905571010")
        parent = create_parent(phone="+998905571011")
        guardian = link_guardian(
            parent=parent,
            student=student,
            relationship="mother",
            custody_notes="Historical custody evidence",
        )

    response = director.delete(f"/api/v1/parents/guardians/{guardian.pk}/")
    assert response.status_code == 204, response.content
    assert director.get(f"/api/v1/parents/guardians/{guardian.pk}/").status_code == 404

    parent_client = as_user(tenant_a, parent.user)
    children = parent_client.get("/api/v1/parents/me/children/")
    assert children.status_code == 200
    assert children.json()["data"] == []
    assert parent_client.get(f"/api/v1/students/{student.pk}/").status_code in {403, 404}

    with schema_context(tenant_a.schema_name):
        guardian = Guardian.objects.get(pk=guardian.pk)
        assert guardian.revoked_at is not None
        assert guardian.custody_notes == "Historical custody evidence"
        assert not RoleMembership.objects.filter(
            user=parent.user,
            role=Role.PARENT,
            revoked_at__isnull=True,
        ).exists()
        replacement = link_guardian(parent=parent, student=student, relationship="mother")
        assert replacement.pk != guardian.pk


def test_pickup_delete_is_audited_deactivation_and_reactivation_fails_closed(
    tenant_a,
    as_role,
):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import PickupAuthorization
    from apps.students.services import create_student

    client, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = create_student(branch=BranchFactory(), phone="+998905571020")
        pickup = PickupAuthorization.objects.create(
            student=student,
            full_name="Authorized adult",
            phone="+998905571021",
        )

    assert client.delete(f"/api/v1/parents/pickups/{pickup.pk}/").status_code == 204
    with schema_context(tenant_a.schema_name):
        pickup.refresh_from_db()
        assert pickup.is_active is False
        assert pickup.deactivated_at is not None
        assert pickup.deactivated_by_id is not None

    reactivation = client.patch(
        f"/api/v1/parents/pickups/{pickup.pk}/",
        {"is_active": True},
        format="json",
    )
    assert reactivation.status_code == 409
    assert reactivation.json()["code"] == "pickup_authorization_inactive"


def test_pickup_student_attribution_is_immutable_and_parent_keeps_a_contact(
    tenant_a,
    as_role,
):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import PickupAuthorization
    from apps.parents.services import create_parent
    from apps.students.services import create_student

    client, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        first_student = create_student(branch=branch, phone="+998905571025")
        second_student = create_student(branch=branch, phone="+998905571026")
        parent = create_parent(
            phone="+998905571027",
            branch_at_creation=branch,
            attribution_status="captured",
        )
        pickup = PickupAuthorization.objects.create(
            student=first_student,
            full_name="Known adult",
            phone="+998905571028",
        )

    moved = client.patch(
        f"/api/v1/parents/pickups/{pickup.pk}/",
        {"student": second_student.pk},
        format="json",
    )
    assert moved.status_code == 400
    assert set(moved.json()["errors"]) == {"student"}
    no_contact = client.patch(
        f"/api/v1/parents/{parent.pk}/",
        {"phone": "", "email": ""},
        format="json",
    )
    assert no_contact.status_code == 400
    assert no_contact.json()["code"] == "identifier_required"
    with schema_context(tenant_a.schema_name):
        with pytest.raises(DatabaseError), transaction.atomic():
            PickupAuthorization.objects.filter(pk=pickup.pk).update(student=second_student)
        pickup.refresh_from_db()
        parent.refresh_from_db()
        assert pickup.student_id == first_student.pk
        assert parent.phone == "+998905571027"


def test_ambiguous_parent_bridge_cannot_receive_a_family_grant_or_be_deactivated(
    tenant_a,
    as_role,
):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.services import create_parent, link_guardian
    from apps.students.services import create_student
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership
    from core.exceptions import ConflictException

    client, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        child = create_student(branch=branch, phone="+998905571035")
        parent = create_parent(phone="+998905571036")
        # A legacy second role on the bridge makes the identity ambiguous. It
        # must not receive a new parent grant or suffer collateral revocation.
        StudentProfileFactory(user=parent.user, branch=branch)

        with pytest.raises(ConflictException) as raised:
            link_guardian(parent=parent, student=child, relationship="mother")
        assert getattr(raised.value, "code", "") == "identity_bridge_ambiguous"

    denied = client.delete(f"/api/v1/parents/{parent.pk}/")
    assert denied.status_code == 409
    assert denied.json()["code"] == "identity_bridge_ambiguous"
    with schema_context(tenant_a.schema_name):
        parent.refresh_from_db()
        parent.user.refresh_from_db()
        assert parent.is_active is True
        assert parent.user.is_active is True
        assert not RoleMembership.objects.filter(
            user=parent.user,
            role=Role.PARENT,
            revoked_at__isnull=True,
        ).exists()


def test_family_mutations_reject_unknown_fields(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import PickupAuthorization
    from apps.parents.services import create_parent
    from apps.students.services import create_student

    client, _actor = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        parent = create_parent(
            phone="+998905571030",
            branch_at_creation=branch,
            attribution_status="captured",
        )
        student = create_student(branch=branch, phone="+998905571031")
        pickup = PickupAuthorization.objects.create(
            student=student,
            full_name="Known adult",
            phone="+998905571032",
        )

    parent_response = client.patch(
        f"/api/v1/parents/{parent.pk}/",
        {"is_active": False},
        format="json",
    )
    pickup_response = client.patch(
        f"/api/v1/parents/pickups/{pickup.pk}/",
        {"unknown": True},
        format="json",
    )
    guardian_response = client.post(
        "/api/v1/parents/guardians/",
        {
            "parent": parent.pk,
            "student": student.pk,
            "relationship": "mother",
            "role": "owner",
        },
        format="json",
    )
    assert parent_response.status_code == pickup_response.status_code == guardian_response.status_code == 400
    assert set(parent_response.json()["errors"]) == {"is_active"}
    assert set(pickup_response.json()["errors"]) == {"unknown"}
    assert set(guardian_response.json()["errors"]) == {"role"}


@pytest.mark.django_db(transaction=True)
def test_database_rejects_direct_family_history_deletion(tenant_a):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import Guardian, ParentProfile, PickupAuthorization
    from apps.parents.services import create_parent, link_guardian
    from apps.students.services import create_student

    with schema_context(tenant_a.schema_name):
        student = create_student(branch=BranchFactory(), phone="+998905571040")
        parent = create_parent(phone="+998905571041")
        guardian = link_guardian(parent=parent, student=student, relationship="father")
        pickup = PickupAuthorization.objects.create(
            student=student,
            full_name="Historical adult",
            phone="+998905571042",
        )
        for model, pk in (
            (ParentProfile, parent.pk),
            (Guardian, guardian.pk),
            (PickupAuthorization, pickup.pk),
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                model.objects.filter(pk=pk).delete()
