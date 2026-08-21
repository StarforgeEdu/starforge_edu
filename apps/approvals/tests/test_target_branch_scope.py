"""Approval targets must never borrow authority from another branch."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.exceptions import NotFoundException
from core.permissions import Role

pytestmark = pytest.mark.django_db

REQUESTS = "/api/v1/approvals/requests/"


def _target_request(tenant, *, kind: str, branch, user_in) -> dict:
    """Build one valid target-bearing generic request in ``branch``."""
    borrower = user_in(tenant, roles=[Role.TEACHER], branch=branch) if kind == "loan" else None
    with schema_context(tenant.schema_name):
        from apps.students.tests.factories import StudentProfileFactory

        student = StudentProfileFactory.create(branch=branch)
        if kind == "discount":
            return {
                "kind": kind,
                "title": "Scoped discount",
                "payload": {"student_id": student.pk, "percent": "10"},
            }
        if kind == "fine":
            return {
                "kind": kind,
                "title": "Scoped fine",
                "amount_uzs": "100.00",
                "payload": {"student_id": student.pk, "reason": "Test"},
            }
        if kind == "payment_delay":
            from apps.finance.models import Invoice
            from apps.finance.tests.factories import InvoiceFactory

            due_date = timezone.localdate() + timedelta(days=5)
            invoice = InvoiceFactory.create(
                student=student,
                due_date=due_date,
                status=Invoice.Status.ISSUED,
            )
            return {
                "kind": kind,
                "title": "Scoped delay",
                "payload": {
                    "invoice_id": invoice.pk,
                    "new_due_date": (due_date + timedelta(days=10)).isoformat(),
                },
            }
        if kind == "absence_deduction":
            from apps.attendance.models import AttendanceRecord
            from apps.attendance.tests.factories import AttendanceRecordFactory
            from apps.cohorts.tests.factories import CohortFactory
            from apps.org.models import CenterSettings
            from apps.schedule.models import Lesson
            from apps.schedule.tests.factories import TermFactory
            from apps.teachers.tests.factories import TeacherProfileFactory

            settings = CenterSettings.load()
            settings.absence_deduction_enabled = True
            settings.absence_deduction_excused_only = False
            settings.save()
            starts_at = timezone.now() + timedelta(days=1)
            lesson = Lesson.objects.create(
                term=TermFactory.create(),
                cohort=CohortFactory.create(branch=branch),
                teacher=TeacherProfileFactory.create(branch=branch),
                title="Scope test",
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
            )
            record = AttendanceRecordFactory.create(
                student=student,
                lesson=lesson,
                status=AttendanceRecord.Status.ABSENT,
            )
            return {
                "kind": kind,
                "title": "Scoped absence credit",
                "payload": {
                    "student_id": student.pk,
                    "attendance_id": record.pk,
                    "fixed_amount_uzs": "100.00",
                },
            }

    assert borrower is not None
    return {
        "kind": kind,
        "title": "Scoped loan",
        "amount_uzs": "100.00",
        "payload": {"borrower_id": borrower.pk},
    }


@pytest.mark.parametrize(
    "kind",
    ["discount", "fine", "absence_deduction", "payment_delay", "loan"],
)
def test_target_kinds_reject_cross_branch_targets_in_domain_and_api(
    tenant_a,
    user_in,
    as_user,
    kind,
):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
    requester = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    client = as_user(tenant_a, requester)
    body = _target_request(tenant_a, kind=kind, branch=branch_b, user_in=user_in)

    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.approvals.services import create_request

        with pytest.raises(NotFoundException) as exc_info:
            create_request(
                kind=body["kind"],
                title=body["title"],
                requested_by=requester,
                amount_uzs=(Decimal(body["amount_uzs"]) if body.get("amount_uzs") is not None else None),
                branch=branch_a,
                payload=body.get("payload", {}),
                allowed_branch_ids={branch_a.pk},
            )
        assert exc_info.value.code == "not_found"
        assert ApprovalRequest.objects.count() == 0

    response = client.post(
        REQUESTS,
        {**body, "branch": branch_a.pk},
        format="json",
    )
    assert response.status_code == 404, response.content
    assert response.json()["code"] == "not_found"
    with schema_context(tenant_a.schema_name):
        assert ApprovalRequest.objects.count() == 0


@pytest.mark.parametrize("kind", ["expense", "procurement", "event_split", "book_cash", "other"])
def test_untargeted_generic_kinds_enforce_allowed_branch_in_domain(
    tenant_a,
    user_in,
    kind,
):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.approvals.services import create_request

        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
        requester = user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a)
        with pytest.raises(NotFoundException):
            create_request(
                kind=kind,
                title="Cross-branch generic request",
                requested_by=requester,
                amount_uzs=Decimal("10.00"),
                branch=branch_b,
                allowed_branch_ids={branch_a.pk},
            )
        assert ApprovalRequest.objects.count() == 0


def test_loan_product_surface_uses_the_same_target_scope_boundary(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
    requester = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    borrower = user_in(tenant_a, roles=[Role.TEACHER], branch=branch_b)

    response = as_user(tenant_a, requester).post(
        "/api/v1/loans/",
        {
            "title": "Cross-branch loan",
            "amount_uzs": "100.00",
            "branch": branch_a.pk,
            "borrower": borrower.pk,
        },
        format="json",
    )
    assert response.status_code == 404, response.content
    assert response.json()["code"] == "not_found"
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest

        assert not ApprovalRequest.objects.filter(kind="loan").exists()


def test_student_transfer_before_approval_blocks_effect_and_preserves_pending_request(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        from apps.students.tests.factories import StudentProfileFactory

        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
        student = StudentProfileFactory.create(branch=branch_a)
    requester = user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a)
    approver = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    requester_client = as_user(tenant_a, requester)
    approver_client = as_user(tenant_a, approver)

    created = requester_client.post(
        REQUESTS,
        {
            "kind": "discount",
            "title": "Transfer race",
            "payload": {"student_id": student.pk, "percent": "10"},
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    request_id = created.json()["data"]["id"]
    assert created.json()["data"]["branch"] == branch_a.pk

    with schema_context(tenant_a.schema_name):
        from apps.students.models import StudentProfile

        StudentProfile.objects.filter(pk=student.pk).update(branch=branch_b)

    blocked = approver_client.post(f"{REQUESTS}{request_id}/approve/", {}, format="json")
    assert blocked.status_code == 404, blocked.content
    assert blocked.json()["code"] == "not_found"
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.finance.models import Discount

        assert ApprovalRequest.objects.get(pk=request_id).status == ApprovalRequest.Status.PENDING
        assert not Discount.objects.filter(student_id=student.pk).exists()


def test_borrower_transfer_before_disbursement_blocks_ledger_effect(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
    requester = user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a)
    borrower = user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a)
    approver = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    cashier = user_in(tenant_a, roles=[Role.CASHIER], branch=branch_a)

    created = as_user(tenant_a, requester).post(
        REQUESTS,
        {
            "kind": "loan",
            "title": "Branch-specific loan",
            "amount_uzs": "100.00",
            "payload": {"borrower_id": borrower.pk},
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    request_id = created.json()["data"]["id"]
    approved = as_user(tenant_a, approver).post(
        f"{REQUESTS}{request_id}/approve/",
        {},
        format="json",
    )
    assert approved.status_code == 200, approved.content

    with schema_context(tenant_a.schema_name):
        from apps.finance.models import PaymentMethod
        from apps.users.models import RoleMembership

        RoleMembership.objects.filter(user=borrower, revoked_at__isnull=True).delete()
        RoleMembership.objects.create(user=borrower, branch=branch_b, role=Role.TEACHER)
        method = PaymentMethod.objects.create(name="Scoped cash", slug="scoped-cash")

    blocked = as_user(tenant_a, cashier).post(
        f"{REQUESTS}{request_id}/disburse/",
        {"payment_method": method.pk},
        format="json",
    )
    assert blocked.status_code == 404, blocked.content
    assert blocked.json()["code"] == "not_found"
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest, LedgerEntry

        assert ApprovalRequest.objects.get(pk=request_id).status == ApprovalRequest.Status.APPROVED
        assert not LedgerEntry.objects.filter(source_kind="approval_request", source_id=request_id).exists()


def test_student_transfer_before_reject_preserves_approved_effect(
    tenant_a,
    user_in,
    as_user,
):
    """An old-branch approver cannot reverse a transferred student's discount."""
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        from apps.students.tests.factories import StudentProfileFactory

        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
        student = StudentProfileFactory.create(branch=branch_a)
    requester = user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a)
    approver = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    requester_client = as_user(tenant_a, requester)
    approver_client = as_user(tenant_a, approver)

    created = requester_client.post(
        REQUESTS,
        {
            "kind": "discount",
            "title": "Transfer after approval",
            "payload": {"student_id": student.pk, "percent": "10"},
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    request_id = created.json()["data"]["id"]
    approved = approver_client.post(
        f"{REQUESTS}{request_id}/approve/",
        {},
        format="json",
    )
    assert approved.status_code == 200, approved.content
    discount_id = approved.json()["data"]["payload"]["discount_id"]

    with schema_context(tenant_a.schema_name):
        from apps.students.models import StudentProfile

        StudentProfile.objects.filter(pk=student.pk).update(branch=branch_b)

    blocked = approver_client.post(
        f"{REQUESTS}{request_id}/reject/",
        {"note": "No longer in my branch"},
        format="json",
    )
    assert blocked.status_code == 404, blocked.content
    assert blocked.json()["code"] == "not_found"
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.finance.models import Discount

        assert ApprovalRequest.objects.get(pk=request_id).status == ApprovalRequest.Status.APPROVED
        assert Discount.objects.get(pk=discount_id).is_active is True
