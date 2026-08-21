from __future__ import annotations

from datetime import date

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.crm.dto import LeadFilterDTO
from apps.crm.models import CRMLead, LeadDuplicateCandidate, LeadMerge
from apps.crm.presenters import lead_to_dict
from apps.crm.repositories.crm_repository import CRMRepository
from apps.crm.services.v1.crm_service import CRMService
from apps.crm.tests.helpers import create_lead, lead_student, stage
from apps.org.tests.factories import BranchFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db


def _branch_of(user):
    return user.role_memberships.get(revoked_at__isnull=True).branch


def _matching_students(branch):
    birthdate = date(2011, 4, 3)
    return (
        lead_student(branch=branch, first_name="Dilnoza", last_name="Rasulova", birthdate=birthdate),
        lead_student(branch=branch, first_name="Dilnoza", last_name="Rasulova", birthdate=birthdate),
    )


def test_duplicate_detection_and_reviewed_merge_preserve_both_identities_and_history(tenant_a, as_role):
    client, manager = as_role(Role.HEAD_OF_DEPT)
    with schema_context(tenant_a.schema_name):
        first_student, second_student = _matching_students(_branch_of(manager))
    first = create_lead(client, first_student, key="duplicate-first-lead")
    second = create_lead(client, second_student, key="duplicate-second-lead")
    assert first.status_code == second.status_code == 201
    first_id = first.json()["data"]["id"]
    second_id = second.json()["data"]["id"]

    detected = client.post(
        f"/api/v1/crm/leads/{first_id}/detect-duplicates/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="duplicate-scan-0001",
    )
    assert detected.status_code == 200, detected.content
    assert detected["Idempotency-Replayed"] == "false"
    rows = detected.json()["data"]
    assert len(rows) == 1
    assert rows[0]["score"] == 80
    assert rows[0]["signals"] == ["name_birthdate"]
    candidate_id = rows[0]["id"]

    replay = client.post(
        f"/api/v1/crm/leads/{first_id}/detect-duplicates/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="duplicate-scan-0001",
    )
    assert replay.status_code == 200
    assert replay["Idempotency-Replayed"] == "true"
    assert replay.json()["data"] == rows

    merged = client.post(
        f"/api/v1/crm/duplicates/{candidate_id}/merge/",
        {
            "canonical_lead": first_id,
            "rationale": "Same legal identity confirmed by admissions staff.",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="duplicate-merge-0001",
    )
    assert merged.status_code == 200, merged.content
    assert merged.json()["data"]["canonical_lead"] == first_id
    assert merged.json()["data"]["duplicate_lead"] == second_id

    with schema_context(tenant_a.schema_name):
        canonical = CRMLead.objects.get(pk=first_id)
        duplicate = CRMLead.objects.get(pk=second_id)
        assert canonical.state == CRMLead.State.OPEN
        assert duplicate.state == CRMLead.State.MERGED
        assert duplicate.canonical_lead_id == canonical.pk
        assert duplicate.student_id == second_student.pk
        assert duplicate.student.is_active is False
        assert canonical.student_id == first_student.pk
        assert canonical.stage_history.count() == duplicate.stage_history.count() == 1
        assert LeadMerge.objects.filter(candidate_id=candidate_id).count() == 1
        assert LeadDuplicateCandidate.objects.get(pk=candidate_id).status == "merged"


def test_duplicate_review_requires_both_leads_inside_current_scope(tenant_a, as_role, user_in, as_user):
    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        first, second = _matching_students(branch_a)
        # Move one matching identity to a different immutable CRM scope.
        second.branch = branch_b
        second.save(update_fields=("branch", "updated_at"))
    first_id = create_lead(director, first, key="cross-scope-duplicate-a").json()["data"]["id"]
    create_lead(director, second, key="cross-scope-duplicate-b")
    scan = director.post(
        f"/api/v1/crm/leads/{first_id}/detect-duplicates/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="cross-scope-scan-01",
    )
    candidate_id = scan.json()["data"][0]["id"]
    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    manager_client = as_user(tenant_a, manager)
    assert manager_client.get("/api/v1/crm/duplicates/").json()["data"] == []
    denied = manager_client.post(
        f"/api/v1/crm/duplicates/{candidate_id}/dismiss/",
        {"rationale": "scope probe"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="cross-scope-dismiss",
    )
    assert denied.status_code == 404


def test_funnel_has_explicit_window_units_scope_and_source_filter(tenant_a, as_role):
    client, manager = as_role(Role.HEAD_OF_DEPT)
    with schema_context(tenant_a.schema_name):
        branch = _branch_of(manager)
        students = [lead_student(branch=branch, first_name=f"Lead{index}") for index in range(3)]
        contacted_id = stage("contacted").pk
        converted_id = stage("converted").pk
        lost_id = stage("lost").pk
    lead_ids = [
        create_lead(client, student, key=f"funnel-lead-{index:02d}").json()["data"]["id"]
        for index, student in enumerate(students)
    ]
    moved = client.post(
        f"/api/v1/crm/leads/{lead_ids[0]}/transition/",
        {"stage": contacted_id, "expected_version": 1},
        format="json",
        HTTP_IDEMPOTENCY_KEY="funnel-contacted",
    )
    assert moved.status_code == 200
    won = client.post(
        f"/api/v1/crm/leads/{lead_ids[0]}/transition/",
        {"stage": converted_id, "expected_version": 2},
        format="json",
        HTTP_IDEMPOTENCY_KEY="funnel-won",
    )
    lost = client.post(
        f"/api/v1/crm/leads/{lead_ids[1]}/transition/",
        {"stage": lost_id, "expected_version": 1, "loss_reason": "Schedule mismatch"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="funnel-lost",
    )
    assert won.status_code == lost.status_code == 200

    today = timezone.localdate().isoformat()
    response = client.get(f"/api/v1/crm/funnel/?date_from={today}&date_to={today}&branch={branch.pk}")
    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["window"] == {
        "date_from": today,
        "date_to": today,
        "timezone": "Asia/Tashkent",
        "basis": "lead_created_at",
        "inclusive": True,
    }
    assert data["scope"]["authorization"]["branch_wide"] == [branch.pk]
    assert data["scope"]["filters"]["branch"] == branch.pk
    assert data["sample_size"] == 3
    assert data["excluded_merged_count"] == 0
    assert data["states"] == {"open": 1, "won": 1, "lost": 1}
    assert data["conversion_fraction"] == pytest.approx(1 / 3)
    assert data["loss_fraction"] == pytest.approx(1 / 3)
    assert sum(stage_row["count"] for stage_row in data["stages"]) == 3


def test_lead_listing_and_funnel_query_counts_are_population_bounded(
    tenant_a, as_role, django_assert_num_queries
):
    client, manager = as_role(Role.HEAD_OF_DEPT)
    with schema_context(tenant_a.schema_name):
        branch = _branch_of(manager)
        students = [lead_student(branch=branch, first_name=f"Perf{index}") for index in range(12)]
    for index, student in enumerate(students):
        response = create_lead(client, student, key=f"performance-lead-{index:02d}")
        assert response.status_code == 201

    service = CRMService(CRMRepository())
    from apps.crm.dto import CRMScope

    scope = CRMScope(branch_wide_ids=frozenset({branch.pk}))
    with schema_context(tenant_a.schema_name):
        with django_assert_num_queries(1):
            rows = [lead_to_dict(row) for row in service.leads(scope=scope, filters=LeadFilterDTO())]
        assert len(rows) == 12
        today = timezone.localdate()
        with django_assert_num_queries(2):
            summary = service.funnel(
                scope=scope,
                date_from=today,
                date_to=today,
                branch_id=branch.pk,
                department_id=None,
                source_id=None,
                campaign_id=None,
            )
        assert summary["sample_size"] == 12
