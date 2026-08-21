"""F14-1 — expense lifecycle: create -> approve -> pay (chosen method), with
state-machine + permission gating."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

PM_URL = "/api/v1/finance/payment-methods/"
EXP_URL = "/api/v1/finance/expenses/"


def test_expense_create_approve_pay_uses_three_people_and_ledger(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    maker = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch)
    checker = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    payer = user_in(tenant_a, roles=[Role.CASHIER], branch=branch)
    owner = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)
    maker_client = as_user(tenant_a, maker)
    checker_client = as_user(tenant_a, checker)
    payer_client = as_user(tenant_a, payer)
    owner_client = as_user(tenant_a, owner)

    # Payment methods are organization-wide configuration. A branch accountant
    # can use one for an expense but cannot redefine the global catalog.
    pm = owner_client.post(PM_URL, {"name": "Cash"}, format="json")
    assert pm.status_code == 201, pm.content
    assert pm.json()["data"]["slug"] == "cash"
    method_id = pm.json()["data"]["id"]

    exp = maker_client.post(
        EXP_URL,
        {"branch": branch.pk, "description": "Markers", "amount_uzs": "50000.00", "category": "supplies"},
        format="json",
    )
    assert exp.status_code == 201, exp.content
    eid = exp.json()["data"]["id"]
    assert exp.json()["data"]["status"] == "pending"
    approval_id = exp.json()["data"]["approval_request"]

    # cannot pay before approval
    early = payer_client.post(f"{EXP_URL}{eid}/pay/", {"payment_method": method_id}, format="json")
    assert early.status_code == 422

    # Strict maker-checker applies even though the maker also has approve rights.
    assert maker_client.post(f"{EXP_URL}{eid}/approve/", {}, format="json").status_code == 403

    ap = checker_client.post(f"{EXP_URL}{eid}/approve/", {}, format="json")
    assert ap.status_code == 200
    assert ap.json()["data"]["status"] == "approved"

    paid = payer_client.post(f"{EXP_URL}{eid}/pay/", {"payment_method": method_id}, format="json")
    assert paid.status_code == 200
    assert paid.json()["data"]["status"] == "paid"
    assert paid.json()["data"]["payment_method"] == method_id
    assert paid.json()["data"]["ledger_entry"] is not None

    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest, LedgerEntry

        request = ApprovalRequest.objects.get(pk=approval_id)
        assert request.status == ApprovalRequest.Status.DISBURSED
        entry = LedgerEntry.objects.get(pk=request.ledger_entry_id)
        assert entry.direction == LedgerEntry.Direction.OUT
        assert entry.entry_type == "expense"
        assert entry.amount_uzs == 50000

    # re-approving a paid expense is rejected
    assert checker_client.post(f"{EXP_URL}{eid}/approve/", {}, format="json").status_code == 422


def test_expense_reject(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    maker = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch)
    checker = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    maker_client = as_user(tenant_a, maker)
    checker_client = as_user(tenant_a, checker)
    eid = maker_client.post(
        EXP_URL, {"branch": branch.pk, "description": "x", "amount_uzs": "10.00"}, format="json"
    ).json()["data"]["id"]
    resp = checker_client.post(f"{EXP_URL}{eid}/reject/", {"reason": "no budget"}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "rejected"
    assert resp.json()["data"]["reject_reason"] == "no budget"


def test_teacher_can_raise_expense_but_cannot_approve_it(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    teacher = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    client = as_user(tenant_a, teacher)
    resp = client.post(
        EXP_URL, {"branch": branch.pk, "description": "x", "amount_uzs": "1.00"}, format="json"
    )
    assert resp.status_code == 201
    assert client.post(f"{EXP_URL}{resp.json()['data']['id']}/approve/", {}, format="json").status_code == 403
