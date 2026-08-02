from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from .helpers import make_actor, make_period, make_teacher

pytestmark = pytest.mark.django_db


def _client_for_actor(*, actor, tenant, client_for):
    from core.session_auth import create_session

    with schema_context(tenant.schema_name):
        session = create_session(
            actor.user,
            principal_kind=actor.principal.kind,
            principal_id=actor.principal.principal_id,
        )
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
    return client


def test_decision_registers_reject_unknown_duplicate_and_empty_filters(tenant_a, client_for):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        reader = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:run"),
        )
        make_period(actor=reader, branch=branch)
    client = _client_for_actor(actor=reader, tenant=tenant_a, client_for=client_for)

    unknown = client.get("/api/v1/payroll/periods/?brnach=1")
    duplicate = client.get("/api/v1/payroll/periods/?branch=1&branch=2")
    empty = client.get("/api/v1/payroll/periods/?status=")
    oversized = client.get("/api/v1/payroll/periods/?page_size=101")

    for response in (unknown, duplicate, empty, oversized):
        assert response.status_code == 400, response.content
        assert response.json()["success"] is False
        assert response.json()["code"] == "validation_error"


def test_out_of_scope_period_detail_is_not_found(tenant_a, client_for):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        own = DepartmentFactory(branch=branch)
        other = DepartmentFactory(branch=branch)
        reader = make_actor(
            branch=branch,
            department=own,
            permissions=("compensation:read", "compensation:run"),
        )
        other_runner = make_actor(
            branch=branch,
            department=other,
            permissions=("compensation:read", "compensation:run"),
        )
        hidden = make_period(
            actor=other_runner,
            branch=branch,
            department=other,
            label="Hidden payroll",
        )
    client = _client_for_actor(actor=reader, tenant=tenant_a, client_for=client_for)
    response = client.get(f"/api/v1/payroll/periods/{hidden.pk}/")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_money_mutations_require_decimal_strings_and_closed_dtos(tenant_a, client_for):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        writer = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:write"),
        )
        teacher = make_teacher(branch=branch)
    client = _client_for_actor(actor=writer, tenant=tenant_a, client_for=client_for)
    base = {
        "teacher": teacher.pk,
        "kind": "bonus",
        "amount_uzs": 1000.25,
        "currency": "UZS",
        "effective_period_start": "2026-06-01",
        "effective_period_end": "2026-06-30",
        "reason": "Evidence",
    }
    numeric = client.post(
        "/api/v1/payroll/adjustments/",
        base,
        format="json",
        HTTP_IDEMPOTENCY_KEY="http-adjustment-0001",
    )
    assert numeric.status_code == 400
    assert "amount_uzs" in numeric.json()["errors"]

    scientific = client.post(
        "/api/v1/payroll/adjustments/",
        {**base, "amount_uzs": "1e3"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="http-adjustment-0003",
    )
    assert scientific.status_code == 400
    assert "amount_uzs" in scientific.json()["errors"]

    unknown = client.post(
        "/api/v1/payroll/adjustments/",
        {**base, "amount_uzs": "1000.25", "approved": True},
        format="json",
        HTTP_IDEMPOTENCY_KEY="http-adjustment-0002",
    )
    assert unknown.status_code == 400
    assert "approved" in unknown.json()["errors"]


def test_misdocumented_read_regression_is_impossible_for_payroll_actions(tenant_a, client_for):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        reader = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:approve"),
        )
        period = make_period(
            actor=make_actor(
                branch=branch,
                permissions=("compensation:read", "compensation:run"),
            ),
            branch=branch,
        )
    client = _client_for_actor(actor=reader, tenant=tenant_a, client_for=client_for)
    response = client.get(f"/api/v1/payroll/periods/{period.pk}/approve/")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


def test_teacher_self_payslips_are_exact_principal_scoped(tenant_a, client_for):
    from apps.org.tests.factories import BranchFactory
    from apps.payroll.dto import PreviewFilterDTO
    from apps.payroll.models import PayrollPayslip
    from apps.payroll.services import approve_period, run_period
    from core.session_auth import create_session

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
            permissions=("compensation:disburse",),
        )
        own_teacher = make_teacher(branch=branch, amount="700000.00")
        other_teacher = make_teacher(branch=branch, amount="800000.00")
        period = run_period(
            period=make_period(actor=maker, branch=branch),
            filters=PreviewFilterDTO((own_teacher.pk, other_teacher.pk)),
            actor=maker.user,
            principal=maker.principal,
            idempotency_key="teacher-self-run-0001",
        )
        approve_period(
            period=period,
            actor=checker.user,
            principal=checker.principal,
            note="Approved",
            idempotency_key="teacher-self-approve-1",
        )
        own_payslip = PayrollPayslip.objects.get(line_item__teacher=own_teacher)
        other_payslip = PayrollPayslip.objects.get(line_item__teacher=other_teacher)
        session = create_session(
            own_teacher.user,
            principal_kind="teacher",
            principal_id=own_teacher.pk,
        )
        cashier_session = create_session(
            cashier.user,
            principal_kind="staff",
            principal_id=cashier.staff.pk,
        )

    cashier_client = client_for(tenant_a)
    cashier_client.credentials(HTTP_AUTHORIZATION=f"Bearer {cashier_session.key}")
    instructions = cashier_client.get("/api/v1/payroll/disbursements/")
    assert instructions.status_code == 200
    assert len(instructions.json()["data"]) == 2
    assert set(instructions.json()["data"][0]) == {
        "line_item",
        "period",
        "period_label",
        "pay_date",
        "payslip",
        "payslip_number",
        "teacher",
        "teacher_code",
        "teacher_name",
        "branch_at_run",
        "branch_at_run_name",
        "department_at_run",
        "department_at_run_name",
        "amount_uzs",
        "paid_amount_uzs",
        "outstanding_amount_uzs",
        "currency",
    }
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
    mine = client.get("/api/v1/payroll/payslips/mine/")
    own = client.get(f"/api/v1/payroll/payslips/mine/{own_payslip.pk}/")
    hidden = client.get(f"/api/v1/payroll/payslips/mine/{other_payslip.pk}/")
    management = client.get(f"/api/v1/payroll/payslips/{other_payslip.pk}/")

    assert mine.status_code == 200
    assert [row["id"] for row in mine.json()["data"]] == [own_payslip.pk]
    assert own.status_code == 200
    assert hidden.status_code == 404
    assert management.status_code == 403
