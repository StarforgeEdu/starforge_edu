"""Cohort membership invariants (D1-LD-9): moves keep history, archived
cohorts are read-only, and DELETE can never cascade history away."""

from __future__ import annotations

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.cohorts.models import Cohort, CohortMembership
from apps.cohorts.services import enroll_student_in_cohort
from apps.cohorts.tests.factories import CohortFactory
from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db


@pytest.fixture
def director(as_role):
    return as_role(Role.DIRECTOR)[0]


def test_multi_cohort_enrollment_keeps_stable_primary_and_bills_each(
    tenant_a, django_capture_on_commit_callbacks
):
    """R2-04 (product decision — multi-cohort IS a feature): a student may hold multiple
    simultaneous active cohort memberships (e.g. English + Math). A secondary enroll adds
    a membership and bills that cohort's own fee schedule, but does NOT silently reassign
    current_cohort — the PRIMARY stays the first enrollment (a MOVE is the explicit way to
    change it)."""
    from decimal import Decimal

    from apps.finance.models import Invoice
    from apps.finance.tests.factories import FeeScheduleFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort_a = CohortFactory.create(branch=branch)
        cohort_b = CohortFactory.create(branch=branch)
        FeeScheduleFactory(cohort=cohort_a, amount_uzs=Decimal("100000.00"))
        FeeScheduleFactory(cohort=cohort_b, amount_uzs=Decimal("60000.00"))
        student = StudentProfileFactory.create(branch=branch)
        # execute=True runs the on_commit auto-issue so per-cohort invoices materialize.
        with django_capture_on_commit_callbacks(execute=True):
            enroll_student_in_cohort(cohort=cohort_a, student=student)
        with django_capture_on_commit_callbacks(execute=True):
            enroll_student_in_cohort(cohort=cohort_b, student=student)

        # BOTH memberships are active (multi-cohort feature).
        active = set(
            CohortMembership.objects.filter(student=student, end_date__isnull=True).values_list(
                "cohort_id", flat=True
            )
        )
        assert active == {cohort_a.id, cohort_b.id}
        # PRIMARY stays the first cohort — a secondary enroll must not silently flip it.
        student.refresh_from_db()
        assert student.current_cohort_id == cohort_a.id
        # Each cohort billed on its own fee schedule (per-course billing is intended).
        assert Invoice.objects.filter(student=student).count() == 2


def test_cohort_move_keeps_history(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort_a = CohortFactory.create(branch=branch)
        cohort_b = CohortFactory.create(branch=branch)
        student = StudentProfileFactory.create(branch=branch)
        old = enroll_student_in_cohort(cohort=cohort_a, student=student)

    resp = director.post(
        f"/api/v1/cohorts/{cohort_b.id}/move-student/",
        {"student": student.id, "reason": "schedule_conflict"},
        format="json",
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["over_capacity"] is False

    with schema_context(tenant_a.schema_name):
        old.refresh_from_db()  # end-dated, never deleted
        assert old.end_date == timezone.now().date()
        assert old.moved_reason == "schedule_conflict"
        active = CohortMembership.objects.get(student=student, end_date__isnull=True)
        assert active.cohort_id == cohort_b.id
        student.refresh_from_db()
        assert student.current_cohort_id == cohort_b.id
        assert CohortMembership.objects.filter(student=student).count() == 2


def test_archived_cohort_write_400(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(is_archived=True)

    resp = director.patch(f"/api/v1/cohorts/{cohort.id}/", {"name": "Renamed"}, format="json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "cohort_archived"

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create(branch=cohort.branch)
    resp = director.post(f"/api/v1/cohorts/{cohort.id}/enroll/", {"student": student.id}, format="json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "cohort_archived"


def test_archived_write_precedes_body_validation(director, tenant_a):
    """An archived cohort answers cohort_archived even when the PATCH body has a
    malformed field — the archived guard runs before body parsing (code parity)."""
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(is_archived=True)
    resp = director.patch(
        f"/api/v1/cohorts/{cohort.id}/", {"start_date": "not-a-date"}, format="json"
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "cohort_archived"


def test_destroy_archived_cohort_400(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(is_archived=True)
    resp = director.delete(f"/api/v1/cohorts/{cohort.id}/")
    assert resp.status_code == 400
    assert resp.json()["code"] == "cohort_archived"
    with schema_context(tenant_a.schema_name):
        assert Cohort.objects.filter(pk=cohort.id).exists()


def test_destroy_cohort_with_history_409(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create()
        student = StudentProfileFactory.create(branch=cohort.branch)
        membership = enroll_student_in_cohort(cohort=cohort, student=student)

    resp = director.delete(f"/api/v1/cohorts/{cohort.id}/")
    assert resp.status_code == 409
    assert resp.json()["code"] == "cohort_has_history"
    with schema_context(tenant_a.schema_name):
        assert CohortMembership.objects.filter(pk=membership.id).exists()  # history intact


def test_destroy_empty_cohort_204(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create()
    assert director.delete(f"/api/v1/cohorts/{cohort.id}/").status_code == 204
    with schema_context(tenant_a.schema_name):
        assert not Cohort.objects.filter(pk=cohort.id).exists()


def test_unarchive_makes_cohort_writable_again(director, tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory.create(is_archived=True)

    resp = director.post(f"/api/v1/cohorts/{cohort.id}/unarchive/")
    assert resp.status_code == 200
    assert resp.json()["data"]["is_archived"] is False

    resp = director.patch(f"/api/v1/cohorts/{cohort.id}/", {"name": "Back in service"}, format="json")
    assert resp.status_code == 200


def test_student_patch_cannot_rewrite_cohort_or_branch(director, tenant_a):
    """PATCH /students/{id}/ must not bypass the move service (membership
    history) or the D2 branch-transfer service: both fields are non-writable."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        other_branch = BranchFactory.create()
        cohort_a = CohortFactory.create(branch=branch)
        cohort_b = CohortFactory.create(branch=branch)
        student = StudentProfileFactory.create(branch=branch)
        enroll_student_in_cohort(cohort=cohort_a, student=student)

    resp = director.patch(
        f"/api/v1/students/{student.id}/",
        {"current_cohort": cohort_b.id, "branch": other_branch.id, "academic_level": "B2"},
        format="json",
    )
    assert resp.status_code == 200

    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        assert student.current_cohort_id == cohort_a.id  # unchanged
        assert student.branch_id == branch.id  # unchanged
        assert student.academic_level == "B2"  # writable field applied
        assert CohortMembership.objects.filter(student=student).count() == 1


def test_enroll_reads_current_cohort_from_db_under_lock_not_stale_memory(tenant_a):
    """Regression (self-review, current_cohort NULL-race): enroll must decide the primary
    from the student row it re-reads under select_for_update, NOT a stale in-memory copy.
    A secondary enroll via a handle that still thinks current_cohort is None must not
    silently reset the (committed) primary — the lock is what serialises enroll with a
    concurrent unenroll so a student can never end up active-but-groupless."""
    from apps.students.models import StudentProfile

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort_a = CohortFactory.create(branch=branch, name="A")
        cohort_b = CohortFactory.create(branch=branch, name="B")
        student = StudentProfileFactory.create(branch=branch)
        enroll_student_in_cohort(cohort=cohort_a, student=student)  # primary committed = A

        # A stale handle whose in-memory current_cohort_id is None (pre-fix, enroll trusted
        # this and wrongly reset the primary to B).
        stale = StudentProfile.objects.get(pk=student.pk)
        stale.current_cohort = None
        enroll_student_in_cohort(cohort=cohort_b, student=stale)

        student.refresh_from_db()
        assert student.current_cohort_id == cohort_a.id  # primary unchanged, read from DB


def test_move_student_leaves_exactly_one_active_membership(tenant_a):
    """move_student locks the student row (select_for_update inside its existing
    @transaction.atomic) and must end-date the old membership, leaving the student
    in exactly one active cohort (F2-6)."""
    from apps.cohorts.services import move_student
    from apps.cohorts.tests.factories import CohortMembershipFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort_a = CohortFactory.create(branch=branch, name="A")
        cohort_b = CohortFactory.create(branch=branch, name="B")
        student = StudentProfileFactory.create(branch=branch)
        CohortMembershipFactory.create(cohort=cohort_a, student=student)
        move_student(student=student, to_cohort=cohort_b)
        active = CohortMembership.objects.filter(student=student, end_date__isnull=True)
        assert active.count() == 1
        assert active.get().cohort_id == cohort_b.id
