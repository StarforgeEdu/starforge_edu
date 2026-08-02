from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import DatabaseError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.crm.models import (
    CRMIdempotencyRecord,
    CRMLead,
    LeadFollowUp,
    LeadSource,
    LeadStageHistory,
    LeadTouch,
)
from apps.crm.tests.helpers import create_lead, lead_student, stage
from apps.org.tests.factories import BranchFactory, DepartmentFactory
from apps.students.models import StudentProfile
from apps.users.models import RoleMembership
from core.permissions import Role

pytestmark = pytest.mark.django_db


def _branch_of(user):
    return user.role_memberships.get(revoked_at__isnull=True).branch


def test_lead_pipeline_is_idempotent_audited_and_advances_student_lifecycle(tenant_a, as_role):
    client, manager = as_role(Role.HEAD_OF_DEPT)
    with schema_context(tenant_a.schema_name):
        branch = _branch_of(manager)
        student = lead_student(branch=branch)
        manager_principal = manager.test_principal_id

    created = create_lead(
        client,
        student,
        owner={"kind": "staff", "id": manager_principal},
    )
    assert created.status_code == 201, created.content
    assert created["Idempotency-Replayed"] == "false"
    lead = created.json()["data"]
    assert lead["student"]["id"] == student.pk
    assert lead["owner"]["kind"] == "staff"
    assert lead["version"] == 1

    replay = create_lead(
        client,
        student,
        owner={"kind": "staff", "id": manager_principal},
    )
    assert replay.status_code == 200
    assert replay["Idempotency-Replayed"] == "true"
    assert replay.json()["data"]["id"] == lead["id"]

    with schema_context(tenant_a.schema_name):
        contacted = stage("contacted")
        converted = stage("converted")
    moved = client.post(
        f"/api/v1/crm/leads/{lead['id']}/transition/",
        {"stage": contacted.pk, "expected_version": 1, "note": "Reached guardian"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="transition-contacted-1",
    )
    assert moved.status_code == 200, moved.content
    assert moved.json()["data"]["to_stage"]["slug"] == "contacted"

    won = client.post(
        f"/api/v1/crm/leads/{lead['id']}/transition/",
        {"stage": converted.pk, "expected_version": 2},
        format="json",
        HTTP_IDEMPOTENCY_KEY="transition-converted-1",
    )
    assert won.status_code == 200, won.content
    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        row = CRMLead.objects.get(pk=lead["id"])
        assert student.status == StudentProfile.Status.APPLICATION
        assert row.state == CRMLead.State.WON
        assert row.stage_history.count() == 3
        from apps.audit.models import AuditLog

        assert (
            AuditLog.objects.filter(resource_type="crm.LeadStageHistory", scope_branch_id=branch.pk).count()
            == 2
        )


def test_open_crm_lead_cannot_bypass_pipeline_through_student_transition(tenant_a, as_role):
    client, manager = as_role(Role.HEAD_OF_DEPT)
    with schema_context(tenant_a.schema_name):
        student = lead_student(branch=_branch_of(manager))
    lead_id = create_lead(client, student).json()["data"]["id"]
    direct = client.post(
        f"/api/v1/students/{student.pk}/transition/",
        {"to_status": "application", "reason_code": "", "note": "bypass"},
        format="json",
    )
    assert direct.status_code == 409
    assert direct.json()["code"] == "crm_transition_required"
    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        assert student.status == StudentProfile.Status.LEAD
        assert CRMLead.objects.get(pk=lead_id).state == CRMLead.State.OPEN

        # Signals do not run for QuerySet.update(). The database trigger is the
        # load-bearing guard against bulk/raw lifecycle bypasses.
        with pytest.raises(DatabaseError), transaction.atomic():
            StudentProfile.objects.filter(pk=student.pk).update(status=StudentProfile.Status.APPLICATION)


def test_lead_lifecycle_row_cannot_change_without_same_transaction_history(tenant_a, as_role):
    client, manager = as_role(Role.HEAD_OF_DEPT)
    with schema_context(tenant_a.schema_name):
        student = lead_student(branch=_branch_of(manager))
        contacted = stage("contacted")
    lead_id = create_lead(client, student).json()["data"]["id"]
    with schema_context(tenant_a.schema_name):
        with pytest.raises(DatabaseError), transaction.atomic():
            CRMLead.objects.filter(pk=lead_id).update(stage=contacted)
        lead = CRMLead.objects.get(pk=lead_id)
        assert lead.stage.slug == "new"


def test_lost_stage_requires_reason_and_optimistic_version(tenant_a, as_role):
    client, manager = as_role(Role.HEAD_OF_DEPT)
    with schema_context(tenant_a.schema_name):
        student = lead_student(branch=_branch_of(manager))
        lost = stage("lost")
        contacted_id = stage("contacted").pk
    lead_id = create_lead(client, student).json()["data"]["id"]
    missing = client.post(
        f"/api/v1/crm/leads/{lead_id}/transition/",
        {"stage": lost.pk, "expected_version": 1},
        format="json",
        HTTP_IDEMPOTENCY_KEY="lost-without-reason",
    )
    assert missing.status_code == 400
    assert set(missing.json()["errors"]) == {"loss_reason"}
    lost_response = client.post(
        f"/api/v1/crm/leads/{lead_id}/transition/",
        {"stage": lost.pk, "expected_version": 1, "loss_reason": "Schedule mismatch"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="lost-with-reason",
    )
    assert lost_response.status_code == 200
    direct_after_loss = client.post(
        f"/api/v1/students/{student.pk}/transition/",
        {"to_status": "application", "reason_code": "", "note": "bypass after loss"},
        format="json",
    )
    assert direct_after_loss.status_code == 409
    assert direct_after_loss.json()["code"] == "crm_transition_required"
    stale = client.post(
        f"/api/v1/crm/leads/{lead_id}/transition/",
        {"stage": contacted_id, "expected_version": 1},
        format="json",
        HTTP_IDEMPOTENCY_KEY="stale-transition",
    )
    assert stale.status_code == 409
    reopened = client.post(
        f"/api/v1/crm/leads/{lead_id}/transition/",
        {
            "stage": contacted_id,
            "expected_version": 2,
            "note": "Guardian requested a new schedule review.",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="reopen-lost-lead",
    )
    assert reopened.status_code == 200
    assert reopened.json()["data"]["from_state"] == "lost"
    assert reopened.json()["data"]["to_state"] == "open"


def test_touch_followup_and_attribution_are_append_only_and_retry_safe(tenant_a, as_role):
    client, manager = as_role(Role.HEAD_OF_DEPT)
    with schema_context(tenant_a.schema_name):
        student = lead_student(branch=_branch_of(manager))
        source = LeadSource.objects.get(slug="referral")
    lead_id = create_lead(client, student).json()["data"]["id"]
    touch_payload = {
        "channel": "phone",
        "direction": "outbound",
        "summary": "Discussed available lesson times.",
        "outcome": "interested",
    }
    first = client.post(
        f"/api/v1/crm/leads/{lead_id}/touches/",
        touch_payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="touch-00000001",
    )
    replay = client.post(
        f"/api/v1/crm/leads/{lead_id}/touches/",
        touch_payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="touch-00000001",
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["data"]["id"] == first.json()["data"]["id"]
    mismatch = client.post(
        f"/api/v1/crm/leads/{lead_id}/touches/",
        {**touch_payload, "summary": "Changed payload"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="touch-00000001",
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "idempotency_mismatch"

    due_at = (timezone.now() + timedelta(days=2)).isoformat()
    follow_up = client.post(
        f"/api/v1/crm/leads/{lead_id}/follow-ups/",
        {"due_at": due_at, "purpose": "Confirm placement appointment"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="follow-up-000001",
    )
    assert follow_up.status_code == 201, follow_up.content
    follow_up_id = follow_up.json()["data"]["id"]
    register = client.get(
        f"/api/v1/crm/follow-ups/?status=pending&assignee_kind=staff&assignee_id={manager.test_principal_id}"
    )
    assert register.status_code == 200
    assert [row["id"] for row in register.json()["data"]] == [follow_up_id]
    assert register.json()["data"][0]["lead_summary"]["student"]["id"] == student.pk
    completed = client.post(
        f"/api/v1/crm/follow-ups/{follow_up_id}/complete/",
        {"note": "Appointment confirmed"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="follow-up-done-1",
    )
    assert completed.status_code == 200
    assert completed.json()["data"]["status"] == "completed"

    attribution = client.post(
        f"/api/v1/crm/leads/{lead_id}/attributions/",
        {"source": source.pk, "medium": "guardian-referral"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="attribution-0001",
    )
    assert attribution.status_code == 201
    with schema_context(tenant_a.schema_name):
        assert LeadTouch.objects.filter(lead_id=lead_id).count() == 1
        touch = LeadTouch.objects.get(lead_id=lead_id)
        with pytest.raises(DatabaseError), transaction.atomic():
            LeadTouch.objects.filter(pk=touch.pk).update(outcome="tampered")
        history = LeadStageHistory.objects.filter(lead_id=lead_id).first()
        assert history is not None
        with pytest.raises(DatabaseError), transaction.atomic():
            LeadStageHistory.objects.filter(pk=history.pk).delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            LeadFollowUp.objects.filter(pk=follow_up_id).update(purpose="tampered")


def test_branch_scope_returns_404_for_direct_cross_scope_ids_and_revocation_is_immediate(
    tenant_a, user_in, as_user, as_role
):
    director, _director_user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        student_a = lead_student(branch=branch_a, first_name="A")
        student_b = lead_student(branch=branch_b, first_name="B")
    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    manager_client = as_user(tenant_a, manager)
    lead_a = create_lead(manager_client, student_a, key="branch-a-lead-01")
    assert lead_a.status_code == 201
    hidden_create = create_lead(manager_client, student_b, key="branch-b-probe-01")
    assert hidden_create.status_code == 404
    lead_b = create_lead(director, student_b, key="director-branch-b").json()["data"]
    assert manager_client.get(f"/api/v1/crm/leads/{lead_b['id']}/").status_code == 404
    rows = manager_client.get("/api/v1/crm/leads/").json()["data"]
    assert {row["student"]["id"] for row in rows} == {student_a.pk}

    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.filter(user=manager, revoked_at__isnull=True).update(revoked_at=timezone.now())
    revoked = manager_client.get("/api/v1/crm/leads/")
    assert revoked.status_code == 403


def test_idempotent_replay_rechecks_current_membership_scope(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        student = lead_student(branch=branch_a)
    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    client = as_user(tenant_a, manager)
    key = "scope-bound-replay-0001"
    first = create_lead(client, student, key=key)
    assert first.status_code == 201, first.content
    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.filter(user=manager, revoked_at__isnull=True).update(branch=branch_b)
    replay = create_lead(client, student, key=key)
    assert replay.status_code == 404
    with schema_context(tenant_a.schema_name):
        assert CRMIdempotencyRecord.objects.filter(actor=manager).count() == 1


def test_department_scope_cannot_borrow_another_department_in_same_branch(
    tenant_a, user_in, as_user, as_role
):
    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        dept_a = DepartmentFactory(branch=branch)
        dept_b = DepartmentFactory(branch=branch)
        student_a = lead_student(branch=branch, first_name="DeptA")
        student_b = lead_student(branch=branch, first_name="DeptB")
    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.filter(user=manager, revoked_at__isnull=True).update(department=dept_a)
    manager_client = as_user(tenant_a, manager)
    visible = create_lead(
        manager_client,
        student_a,
        department=dept_a.pk,
        key="dept-a-create-01",
    )
    assert visible.status_code == 201, visible.content
    hidden = create_lead(
        manager_client,
        student_b,
        department=dept_b.pk,
        key="dept-b-probe-01",
    )
    assert hidden.status_code == 404
    director_created = create_lead(
        director,
        student_b,
        department=dept_b.pk,
        key="dept-b-director-1",
    )
    assert director_created.status_code == 201
    assert (
        manager_client.get(f"/api/v1/crm/leads/?branch={branch.pk}&department={dept_b.pk}").status_code == 400
    )


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/v1/crm/leads/?brnach=1", None),
        ("get", "/api/v1/crm/leads/?page=1&page=2", None),
        ("get", "/api/v1/crm/leads/?search=a", None),
        ("post", "/api/v1/crm/leads/", {"student": 1, "stage": 1, "status": "won"}),
    ],
)
def test_decision_registers_reject_unknown_duplicate_or_lifecycle_bypass_input(
    as_role, method, path, payload
):
    client, _ = as_role(Role.HEAD_OF_DEPT)
    response = getattr(client, method)(
        path,
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="strict-input-0001",
    )
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
