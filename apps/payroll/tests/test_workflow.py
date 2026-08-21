from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import DatabaseError, transaction
from django_tenants.utils import schema_context

from apps.payroll.dto import PaymentReconciliationDTO, PreviewFilterDTO, ReversalDTO
from apps.payroll.models import (
    PayrollAdjustment,
    PayrollLineItem,
    PayrollPayslip,
    PayrollPeriod,
    PayrollPeriodEvent,
    PayrollReconciliation,
)
from apps.payroll.services import (
    approve_period,
    preview_period,
    reconcile_payment,
    reverse_payment,
    run_period,
)
from core.exceptions import ConflictException, PermissionException, ValidationException

from .helpers import make_actor, make_period, make_teacher

pytestmark = pytest.mark.django_db


def test_run_freezes_immutable_lines_payslips_and_exact_audit(tenant_a):
    from apps.audit.models import AuditLog
    from apps.org.selectors import get_center_settings
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        maker = make_actor(
            branch=branch,
            department=department,
            permissions=("compensation:read", "compensation:run"),
        )
        teacher = make_teacher(branch=branch, department=department)
        period = make_period(actor=maker, branch=branch, department=department)

        frozen = run_period(
            period=period,
            filters=PreviewFilterDTO((teacher.pk,)),
            actor=maker.user,
            principal=maker.principal,
            idempotency_key="run-june-2026-0001",
        )
        replay = run_period(
            period=frozen,
            filters=PreviewFilterDTO((teacher.pk,)),
            actor=maker.user,
            principal=maker.principal,
            idempotency_key="run-june-2026-0001",
        )

        assert replay.pk == frozen.pk
        assert frozen.status == PayrollPeriod.Status.PENDING_APPROVAL
        assert frozen.organization_timezone == get_center_settings().organization_timezone
        assert frozen.line_count == 1
        assert frozen.net_total_uzs == Decimal("3000000.00")
        assert PayrollLineItem.objects.filter(period=frozen).count() == 1
        payslip = PayrollPayslip.objects.get(line_item__period=frozen)
        assert payslip.snapshot["period"]["organization_timezone"] == frozen.organization_timezone
        assert (
            PayrollPeriodEvent.objects.filter(period=frozen, action=PayrollPeriodEvent.Action.RUN).count()
            == 1
        )
        line = PayrollLineItem.objects.get(period=frozen)
        assert line.branch_at_run_id == branch.pk
        assert line.department_at_run_id == department.pk
        assert line.payout_policy_snapshot["method"] == "flat_monthly"

        with pytest.raises(DatabaseError), transaction.atomic():
            PayrollLineItem.objects.filter(pk=line.pk).update(net_amount_uzs=Decimal("1.00"))
        with pytest.raises(DatabaseError), transaction.atomic():
            PayrollPayslip.objects.filter(line_item=line).delete()

        payroll_audits = AuditLog.objects.filter(resource_type="payroll.PayrollPeriod")
        assert payroll_audits.count() == 2
        assert set(payroll_audits.values_list("actor_attribution_status", flat=True)) == {
            AuditLog.ActorAttributionStatus.EXACT
        }
        assert set(payroll_audits.values_list("actor_principal_kind", flat=True)) == {"staff"}
        assert set(payroll_audits.values_list("actor_principal_id", flat=True)) == {
            maker.principal.principal_id
        }


def test_maker_checker_payment_replay_overpay_and_compensating_reversal(tenant_a):
    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        maker = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:run"),
        )
        checker = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:approve"),
        )
        cashier = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:disburse"),
        )
        teacher = make_teacher(branch=branch, amount="1250000.00")
        period = make_period(actor=maker, branch=branch)
        period = run_period(
            period=period,
            filters=PreviewFilterDTO((teacher.pk,)),
            actor=maker.user,
            principal=maker.principal,
            idempotency_key="run-payment-case-01",
        )
        with pytest.raises(PermissionException, match="maker"):
            approve_period(
                period=period,
                actor=maker.user,
                principal=maker.principal,
                note="",
                idempotency_key="self-approval-key-01",
            )
        period = approve_period(
            period=period,
            actor=checker.user,
            principal=checker.principal,
            note="Reviewed against policy",
            idempotency_key="approve-payment-001",
        )
        method = PaymentMethod.objects.create(name="Payroll bank", slug="payroll-bank")
        line = PayrollLineItem.objects.get(period=period)
        assert period.decided_at is not None
        with pytest.raises(ValidationException) as backdated:
            reconcile_payment(
                period=period,
                dto=PaymentReconciliationDTO(
                    line_item_id=line.pk,
                    amount_uzs=Decimal("1.00"),
                    payment_method_id=method.pk,
                    external_reference="BANK-PAYROLL-BACKDATED",
                    paid_at=period.decided_at - timedelta(seconds=1),
                    idempotency_key="payment-backdated-0001",
                ),
                actor=cashier.user,
                principal=cashier.principal,
            )
        assert backdated.value.code == "validation_error"
        dto = PaymentReconciliationDTO(
            line_item_id=line.pk,
            amount_uzs=Decimal("500000.00"),
            payment_method_id=method.pk,
            external_reference="BANK-PAYROLL-0001",
            paid_at=datetime.now(UTC),
            idempotency_key="payment-reconcile-0001",
        )
        payment = reconcile_payment(
            period=period,
            dto=dto,
            actor=cashier.user,
            principal=cashier.principal,
        )
        replay = reconcile_payment(
            period=period,
            dto=dto,
            actor=cashier.user,
            principal=cashier.principal,
        )
        assert replay.pk == payment.pk
        assert PayrollReconciliation.objects.filter(line_item=line, kind="payment").count() == 1
        period.refresh_from_db()
        assert period.status == PayrollPeriod.Status.PAYMENT_IN_PROGRESS
        assert period.paid_total_uzs == Decimal("500000.00")

        with pytest.raises(ConflictException) as overpay:
            reconcile_payment(
                period=period,
                dto=PaymentReconciliationDTO(
                    line_item_id=line.pk,
                    amount_uzs=Decimal("800000.00"),
                    payment_method_id=method.pk,
                    external_reference="BANK-PAYROLL-OVERPAY",
                    paid_at=datetime.now(UTC),
                    idempotency_key="payment-overpay-0001",
                ),
                actor=cashier.user,
                principal=cashier.principal,
            )
        assert overpay.value.code == "payroll_overpayment"

        reversal_dto = ReversalDTO(
            external_reference="BANK-REVERSAL-0001",
            paid_at=datetime.now(UTC),
            reason="Provider returned the transfer",
            idempotency_key="payment-reversal-0001",
        )
        reversal = reverse_payment(
            reconciliation=payment,
            dto=reversal_dto,
            actor=cashier.user,
            principal=cashier.principal,
        )
        reversal_replay = reverse_payment(
            reconciliation=payment,
            dto=reversal_dto,
            actor=cashier.user,
            principal=cashier.principal,
        )
        assert reversal_replay.pk == reversal.pk
        assert reversal.reverses_id == payment.pk
        period.refresh_from_db()
        assert period.paid_total_uzs == Decimal("0.00")
        assert period.status == PayrollPeriod.Status.APPROVED

        with pytest.raises(DatabaseError), transaction.atomic():
            PayrollReconciliation.objects.filter(pk=payment.pk).update(external_reference="rewritten")


def test_branch_wide_period_conflicts_with_department_period(tenant_a):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        branch_runner = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:run"),
        )
        department_runner = make_actor(
            branch=branch,
            department=department,
            permissions=("compensation:read", "compensation:run"),
        )
        make_period(actor=branch_runner, branch=branch)
        with pytest.raises(ConflictException) as conflict:
            make_period(
                actor=department_runner,
                branch=branch,
                department=department,
            )
        assert conflict.value.code == "payroll_period_overlap"


def test_currency_constraints_reject_raw_non_uzs_rows(tenant_a):
    from django.db import IntegrityError

    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        maker = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:run"),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            PayrollPeriod.objects.create(
                branch=branch,
                label="Invalid currency",
                period_start=datetime(2026, 6, 1).date(),
                period_end=datetime(2026, 6, 30).date(),
                currency="USD",
                organization_timezone="Asia/Tashkent",
                created_by=maker.user,
                created_principal_kind=maker.principal.kind,
                created_principal_id=maker.principal.principal_id,
            )

        assert not PayrollAdjustment.objects.filter(currency="USD").exists()


def test_database_rejects_a_payslip_that_does_not_match_its_line(tenant_a):
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.models import PayoutPolicy

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        maker = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:run"),
        )
        teacher = make_teacher(branch=branch, amount="750000.00")
        policy = PayoutPolicy.objects.get(teacher=teacher)
        period = make_period(actor=maker, branch=branch)

        line = PayrollLineItem.objects.create(
            period=period,
            teacher=teacher,
            branch_at_run=branch,
            department_at_run=None,
            teacher_user_id_snapshot=teacher.user_id,
            teacher_name_snapshot=(teacher.get_full_name() or teacher.username or f"Teacher {teacher.pk}"),
            teacher_code_snapshot=teacher.username or f"teacher-{teacher.pk}",
            payout_policy_id_snapshot=policy.pk,
            payout_method_snapshot=policy.method,
            payout_policy_snapshot={"id": policy.pk, "method": policy.method},
            calculation_breakdown={"flat_amount_uzs": "750000.00"},
            currency="UZS",
            base_amount_uzs=Decimal("750000.00"),
            bonus_amount_uzs=Decimal("0.00"),
            deduction_amount_uzs=Decimal("0.00"),
            net_amount_uzs=Decimal("750000.00"),
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            PayrollPayslip.objects.create(
                line_item=line,
                document_number=f"PAY-{period.pk:08d}-{line.pk:08d}",
                snapshot={"currency": "UZS", "net_amount_uzs": "1.00"},
            )


def test_percent_policy_fails_closed_for_mixed_invoice_allocations(tenant_a):
    from zoneinfo import ZoneInfo

    from apps.cohorts.tests.factories import CohortFactory
    from apps.finance.models import InvoiceLine, PaymentAllocation
    from apps.finance.tests.factories import InvoiceFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.services import set_payout_policy

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        maker = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:run"),
        )
        teacher = make_teacher(branch=branch)
        set_payout_policy(
            teacher=teacher,
            method="percent_of_collected_tuition",
            tuition_percent=Decimal("25.00"),
        )
        cohort = CohortFactory(branch=branch, primary_teacher=teacher)
        lesson_start = datetime(2026, 6, 10, 10, tzinfo=ZoneInfo("Asia/Tashkent"))
        Lesson.objects.create(
            term=TermFactory(),
            cohort=cohort,
            teacher=teacher,
            title="Delivered lesson",
            starts_at=lesson_start,
            ends_at=lesson_start + timedelta(hours=1),
            status=Lesson.Status.COMPLETED,
        )
        invoice = InvoiceFactory(
            student=StudentProfileFactory(branch=branch),
            cohort=cohort,
            total_uzs=Decimal("120000.00"),
        )
        InvoiceLine.objects.bulk_create(
            [
                InvoiceLine(
                    invoice=invoice,
                    description="Tuition",
                    line_type=InvoiceLine.LineType.TUITION,
                    quantity=Decimal("1.00"),
                    unit_price_uzs=Decimal("100000.00"),
                    amount_uzs=Decimal("100000.00"),
                ),
                InvoiceLine(
                    invoice=invoice,
                    description="Book",
                    line_type=InvoiceLine.LineType.MATERIAL,
                    quantity=Decimal("1.00"),
                    unit_price_uzs=Decimal("20000.00"),
                    amount_uzs=Decimal("20000.00"),
                ),
            ]
        )
        allocation = PaymentAllocation.objects.create(
            invoice=invoice,
            payment_id=900001,
            amount_uzs=Decimal("120000.00"),
        )
        PaymentAllocation.objects.filter(pk=allocation.pk).update(created_at=lesson_start)
        period = make_period(actor=maker, branch=branch)

        result = preview_period(
            period=period,
            filters=PreviewFilterDTO((teacher.pk,)),
        )
        assert result["valid"] is False
        assert result["rows"] == []
        assert result["errors"] == [{"teacher": teacher.pk, "code": "ambiguous_tuition_allocation"}]
