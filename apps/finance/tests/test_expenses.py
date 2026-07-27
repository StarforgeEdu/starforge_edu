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


def test_expense_create_approve_pay(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()

    pm = client.post(PM_URL, {"name": "Cash"}, format="json")
    assert pm.status_code == 201, pm.content
    assert pm.json()["data"]["slug"] == "cash"
    method_id = pm.json()["data"]["id"]

    exp = client.post(
        EXP_URL,
        {"branch": branch.pk, "description": "Markers", "amount_uzs": "50000.00", "category": "supplies"},
        format="json",
    )
    assert exp.status_code == 201, exp.content
    eid = exp.json()["data"]["id"]
    assert exp.json()["data"]["status"] == "pending"

    # cannot pay before approval
    early = client.post(f"{EXP_URL}{eid}/pay/", {"payment_method": method_id}, format="json")
    assert early.status_code == 422

    ap = client.post(f"{EXP_URL}{eid}/approve/", {}, format="json")
    assert ap.status_code == 200
    assert ap.json()["data"]["status"] == "approved"

    paid = client.post(f"{EXP_URL}{eid}/pay/", {"payment_method": method_id}, format="json")
    assert paid.status_code == 200
    assert paid.json()["data"]["status"] == "paid"
    assert paid.json()["data"]["payment_method"] == method_id

    # re-approving a paid expense is rejected
    assert client.post(f"{EXP_URL}{eid}/approve/", {}, format="json").status_code == 422


def test_expense_reject(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    eid = client.post(
        EXP_URL, {"branch": branch.pk, "description": "x", "amount_uzs": "10.00"}, format="json"
    ).json()["data"]["id"]
    resp = client.post(f"{EXP_URL}{eid}/reject/", {"reason": "no budget"}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "rejected"
    assert resp.json()["data"]["reject_reason"] == "no budget"


def test_expense_create_requires_finance_write(tenant_a, as_role):
    client, _ = as_role(Role.TEACHER)  # teacher has no finance:write
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = client.post(
        EXP_URL, {"branch": branch.pk, "description": "x", "amount_uzs": "1.00"}, format="json"
    )
    assert resp.status_code == 403
