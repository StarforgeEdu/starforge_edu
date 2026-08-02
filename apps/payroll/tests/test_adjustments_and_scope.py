from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.db import DatabaseError, transaction
from django_tenants.utils import schema_context

from apps.payroll.dto import AdjustmentCreateDTO, PreviewFilterDTO
from apps.payroll.models import PayrollAdjustment, PayrollLineItem, PayrollPeriod
from apps.payroll.repositories import PayrollRepository
from apps.payroll.services import (
    create_adjustment,
    decide_adjustment,
    reject_period,
    run_period,
)
from core.exceptions import ConflictException, PermissionException

from .helpers import make_actor, make_period, make_teacher

pytestmark = pytest.mark.django_db


def test_adjustment_maker_checker_application_release_and_correction(tenant_a):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        maker = make_actor(
            branch=branch,
            department=department,
            permissions=(
                "compensation:read",
                "compensation:write",
                "compensation:run",
            ),
        )
        checker = make_actor(
            branch=branch,
            department=department,
            permissions=("compensation:read", "compensation:approve"),
        )
        teacher = make_teacher(branch=branch, department=department)
        period = make_period(actor=maker, branch=branch, department=department)
        adjustment = create_adjustment(
            dto=AdjustmentCreateDTO(
                teacher_id=teacher.pk,
                kind=PayrollAdjustment.Kind.BONUS,
                amount_uzs=Decimal("250000.00"),
                currency="UZS",
                effective_period_start=date(2026, 6, 1),
                effective_period_end=date(2026, 6, 30),
                reason="Documented substitution coverage",
                idempotency_key="adjustment-create-0001",
            ),
            actor=maker.user,
            principal=maker.principal,
            roles=maker.roles,
        )
        with pytest.raises(PermissionException) as self_decision:
            decide_adjustment(
                adjustment=adjustment,
                approve=True,
                actor=maker.user,
                principal=maker.principal,
                note="",
                idempotency_key="adjustment-self-0001",
            )
        assert self_decision.value.code == "adjustment_self_approval"

        adjustment = decide_adjustment(
            adjustment=adjustment,
            approve=True,
            actor=checker.user,
            principal=checker.principal,
            note="Supporting evidence reviewed",
            idempotency_key="adjustment-approve-01",
        )
        period = run_period(
            period=period,
            filters=PreviewFilterDTO((teacher.pk,)),
            actor=maker.user,
            principal=maker.principal,
            idempotency_key="adjustment-run-000001",
        )
        adjustment.refresh_from_db()
        line = PayrollLineItem.objects.get(period=period)
        assert adjustment.state == PayrollAdjustment.State.APPLIED
        assert adjustment.applied_line_id == line.pk
        assert line.bonus_amount_uzs == Decimal("250000.00")
        assert line.net_amount_uzs == Decimal("3250000.00")

        rejected = reject_period(
            period=period,
            actor=checker.user,
            principal=checker.principal,
            note="The source attendance evidence needs correction",
            idempotency_key="period-reject-00001",
        )
        adjustment.refresh_from_db()
        assert rejected.status == PayrollPeriod.Status.REJECTED
        assert adjustment.state == PayrollAdjustment.State.APPROVED
        assert adjustment.applied_line_id is None

        correction = make_period(
            actor=maker,
            branch=branch,
            department=department,
            correction_of=rejected,
            correction_reason="Rebuilt after attendance correction",
        )
        assert correction.version == rejected.version + 1
        with pytest.raises(ConflictException) as duplicate:
            make_period(
                actor=maker,
                branch=branch,
                department=department,
                correction_of=rejected,
                correction_reason="Duplicate correction",
            )
        assert duplicate.value.code == "payroll_correction_exists"
        correction = run_period(
            period=correction,
            filters=PreviewFilterDTO((teacher.pk,)),
            actor=maker.user,
            principal=maker.principal,
            idempotency_key="correction-run-00001",
        )
        adjustment.refresh_from_db()
        assert adjustment.applied_line.period_id == correction.pk

        with pytest.raises(DatabaseError), transaction.atomic():
            PayrollAdjustment.objects.filter(pk=adjustment.pk).update(decision_reason="rewritten evidence")


def test_repository_intersects_exact_department_scope_and_returns_none_outside(tenant_a):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        own_department = DepartmentFactory(branch=branch)
        other_department = DepartmentFactory(branch=branch)
        own_reader = make_actor(
            branch=branch,
            department=own_department,
            permissions=("compensation:read", "compensation:run"),
        )
        other_runner = make_actor(
            branch=branch,
            department=other_department,
            permissions=("compensation:read", "compensation:run"),
        )
        own_period = make_period(
            actor=own_reader,
            branch=branch,
            department=own_department,
            label="Own department payroll",
        )
        other_period = make_period(
            actor=other_runner,
            branch=branch,
            department=other_department,
            label="Other department payroll",
        )
        repository = PayrollRepository()
        visible = repository.scoped_periods(
            roles=own_reader.roles,
            permission="compensation:read",
        )
        assert list(visible.values_list("pk", flat=True)) == [own_period.pk]
        assert (
            repository.scoped_period(
                roles=own_reader.roles,
                permission="compensation:read",
                period_id=other_period.pk,
            )
            is None
        )


def test_branch_wide_period_collects_department_adjustment(tenant_a):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        runner = make_actor(
            branch=branch,
            permissions=(
                "compensation:read",
                "compensation:write",
                "compensation:run",
            ),
        )
        checker = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:approve"),
        )
        teacher = make_teacher(branch=branch, department=department)
        adjustment = create_adjustment(
            dto=AdjustmentCreateDTO(
                teacher_id=teacher.pk,
                kind=PayrollAdjustment.Kind.DEDUCTION,
                amount_uzs=Decimal("100000.00"),
                currency="UZS",
                effective_period_start=date(2026, 6, 1),
                effective_period_end=date(2026, 6, 30),
                reason="Documented advance repayment",
                idempotency_key="branch-adjustment-001",
            ),
            actor=runner.user,
            principal=runner.principal,
            roles=runner.roles,
        )
        decide_adjustment(
            adjustment=adjustment,
            approve=True,
            actor=checker.user,
            principal=checker.principal,
            note="Reviewed",
            idempotency_key="branch-adj-approve-1",
        )
        period = make_period(actor=runner, branch=branch)
        period = run_period(
            period=period,
            filters=PreviewFilterDTO((teacher.pk,)),
            actor=runner.user,
            principal=runner.principal,
            idempotency_key="branch-wide-run-0001",
        )
        assert PayrollLineItem.objects.get(period=period).deduction_amount_uzs == Decimal("100000.00")
