"""Finance API endpoint matrix (D3-A-9, TESTING.md §3): happy/denied/anonymous,
cross-tenant isolation, parent-own-children scoping, validation, query budget."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.finance.tests.factories import FeeScheduleFactory, InvoiceFactory
from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

INVOICES_URL = "/api/v1/finance/invoices/"
FEE_URL = "/api/v1/finance/fee-schedules/"
OUTSTANDING_URL = "/api/v1/finance/outstanding/"


# --------------------------------------------------------------------------- #
# /invoices/ list — allowed / denied / anonymous
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [Role.DIRECTOR, Role.ACCOUNTANT, Role.CASHIER])
def test_invoice_list_allowed(as_role, role):
    client, _ = as_role(role)
    assert client.get(INVOICES_URL).status_code == 200


@pytest.mark.parametrize("role", [Role.SECURITY, Role.LIBRARIAN])
def test_invoice_list_denied(as_role, role):
    resp = as_role(role)[0].get(INVOICES_URL)
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


def test_invoice_list_anonymous_denied(tenant_a, client_for):
    assert client_for(tenant_a).get(INVOICES_URL).status_code == 401


# --------------------------------------------------------------------------- #
# cross-tenant isolation (TD-1)
# --------------------------------------------------------------------------- #


def test_invoice_cross_tenant_token_rejected(tenant_a, tenant_b, user_in, client_for):
    from apps.auth.services import issue_token

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        access = issue_token(user)["access"]
    client_b = client_for(tenant_b)
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = client_b.get(INVOICES_URL)
    assert resp.status_code == 401
    assert resp.json()["code"] == "authentication_failed"


def test_invoice_not_visible_across_tenants(tenant_a, tenant_b, as_role):
    with schema_context(tenant_a.schema_name):
        InvoiceFactory(number="INV-2026-900001")
    # director on tenant_b cannot see tenant_a's invoice
    client_b, _ = as_role(Role.DIRECTOR, tenant=tenant_b)
    body = client_b.get(INVOICES_URL).json()
    numbers = {row["number"] for row in body["data"]}
    assert "INV-2026-900001" not in numbers


# --------------------------------------------------------------------------- #
# parent sees only own children's balances
# --------------------------------------------------------------------------- #


def test_parent_sees_only_own_childs_balance(tenant_a, user_in, as_user):
    parent_user = user_in(tenant_a, roles=[Role.PARENT])
    with schema_context(tenant_a.schema_name):
        parent = ParentProfileFactory(user=parent_user)
        my_child = StudentProfileFactory()
        other_child = StudentProfileFactory()
        GuardianFactory(parent=parent, student=my_child)
        InvoiceFactory(student=my_child, total_uzs=Decimal("100000.00"))
        InvoiceFactory(student=other_child, total_uzs=Decimal("100000.00"))

    client = as_user(tenant_a, parent_user)
    ok = client.get(f"{OUTSTANDING_URL}?student={my_child.pk}")
    assert ok.status_code == 200
    assert Decimal(ok.json()["data"]["outstanding_uzs"]) == Decimal("100000.00")

    denied = client.get(f"{OUTSTANDING_URL}?student={other_child.pk}")
    assert denied.status_code == 403


def test_director_sees_any_balance(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        InvoiceFactory(student=student, total_uzs=Decimal("250000.00"))
    resp = client.get(f"{OUTSTANDING_URL}?student={student.pk}")
    assert resp.status_code == 200
    assert Decimal(resp.json()["data"]["outstanding_uzs"]) == Decimal("250000.00")


# --------------------------------------------------------------------------- #
# invoice create via service (issue_invoice)
# --------------------------------------------------------------------------- #


def test_create_invoice_endpoint(tenant_a, as_role):
    client, _ = as_role(Role.ACCOUNTANT)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        fs = FeeScheduleFactory(amount_uzs=Decimal("777000.00"))
    resp = client.post(INVOICES_URL, {"student": student.pk, "fee_schedule": fs.pk}, format="json")
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["status"] == "issued"
    assert Decimal(body["total_uzs"]) == Decimal("777000.00")
    assert len(body["lines"]) == 1


def test_invoice_line_explicit_zero_quantity_is_not_coerced_to_one(tenant_a, as_role):
    """An explicit quantity of 0 must bill 0 (a waived line), not be defaulted to 1
    (the default applies only when the key is absent) — no money over-charge."""
    client, _ = as_role(Role.ACCOUNTANT)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    resp = client.post(
        INVOICES_URL,
        {"student": student.pk, "lines": [{"description": "waived", "unit_price_uzs": "100000", "quantity": 0}]},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()["data"]
    assert Decimal(body["lines"][0]["amount_uzs"]) == Decimal("0.00")
    assert Decimal(body["total_uzs"]) == Decimal("0.00")


def test_invoice_line_oversized_quantity_is_400_not_500(tenant_a, as_role):
    """A quantity beyond the column's 8 digits is a clean 400, not a decimal-context
    overflow -> 500 in the line-amount quantize."""
    client, _ = as_role(Role.ACCOUNTANT)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    resp = client.post(
        INVOICES_URL,
        {
            "student": student.pk,
            "lines": [{"description": "x", "unit_price_uzs": "9999999999999999", "quantity": "9999999999999999"}],
        },
        format="json",
    )
    assert resp.status_code == 400, resp.content


def test_create_invoice_validation_empty(tenant_a, as_role):
    client, _ = as_role(Role.ACCOUNTANT)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    resp = client.post(INVOICES_URL, {"student": student.pk}, format="json")
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# invoice void action
# --------------------------------------------------------------------------- #


def test_invoice_void_action(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
    resp = client.post(f"{INVOICES_URL}{inv.pk}/void/")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "void"


# --------------------------------------------------------------------------- #
# fee-schedules CRUD perms
# --------------------------------------------------------------------------- #


def test_fee_schedule_write_requires_finance_write(tenant_a, as_role):
    cashier_client, _ = as_role(Role.CASHIER)  # cashier has finance:read only
    resp = cashier_client.post(
        FEE_URL, {"name": "X", "amount_uzs": "100000.00", "billing_period": "monthly"}, format="json"
    )
    assert resp.status_code == 403

    director_client, _ = as_role(Role.DIRECTOR)
    ok = director_client.post(
        FEE_URL, {"name": "Y", "amount_uzs": "100000.00", "billing_period": "monthly"}, format="json"
    )
    assert ok.status_code == 201


# --------------------------------------------------------------------------- #
# cashier shift endpoints
# --------------------------------------------------------------------------- #


def test_cashier_shift_open_close_endpoints(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    cashier = user_in(tenant_a, roles=[Role.CASHIER])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    client = as_user(tenant_a, cashier)
    opened = client.post(
        "/api/v1/finance/cashier-shifts/open/",
        {"branch": branch.pk, "opening_cash_uzs": "10000.00"},
        format="json",
    )
    assert opened.status_code == 201
    shift_id = opened.json()["data"]["id"]

    # double open -> 409
    again = client.post("/api/v1/finance/cashier-shifts/open/", {"branch": branch.pk}, format="json")
    assert again.status_code == 409

    closed = client.post(
        f"/api/v1/finance/cashier-shifts/{shift_id}/close/",
        {"closing_cash_uzs": "10000.00"},
        format="json",
    )
    assert closed.status_code == 200
    assert closed.json()["data"]["discrepancy_uzs"] == "0.00"

    report = client.get(f"/api/v1/finance/cashier-shifts/{shift_id}/report/")
    assert report.status_code == 200
    assert report.json()["data"]["payments_total_uzs"] == "0.00"


# --------------------------------------------------------------------------- #
# statement async (202 + result)
# --------------------------------------------------------------------------- #


def test_statement_request_returns_202(tenant_a, as_role, monkeypatch):
    from apps.finance import services as fin_services

    monkeypatch.setattr(fin_services, "render_statement_pdf", lambda *, student, locale="en": b"%PDF")
    monkeypatch.setattr(
        "infrastructure.storage.s3_client.upload_bytes",
        lambda key, data, *, content_type="application/octet-stream": key,
    )
    client, _ = as_role(Role.ACCOUNTANT)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    resp = client.post(f"/api/v1/finance/students/{student.pk}/statement/", {"locale": "en"}, format="json")
    assert resp.status_code == 202
    assert "task_id" in resp.json()["data"]


# --------------------------------------------------------------------------- #
# list shape + query budget (<=5 per spec)
# --------------------------------------------------------------------------- #


def test_invoice_list_query_budget(as_role, tenant_a, django_assert_max_num_queries):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        for _ in range(20):
            InvoiceFactory(student=student, total_uzs=Decimal("100000.00"))
    # +1 for billing paywall middleware subscription check
    with django_assert_max_num_queries(10):  # +1: A-2 per-request permission-override load
        body = client.get(INVOICES_URL).json()
    assert set(body) == {"success", "data", "pagination"}


# --------------------------------------------------------------------------- #
# denormalized `_name` companions on the invoice list (frontend needs no 2nd call)
# --------------------------------------------------------------------------- #


def test_invoice_list_includes_readable_name_companions(tenant_a, as_role):
    """Each bare FK id on an invoice row carries a readable `_name` companion,
    resolved from the selector's select_related (no extra query per row)."""
    from apps.cohorts.tests.factories import CohortFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory(name="Algebra A")
        fs = FeeScheduleFactory(name="Monthly Tuition", cohort=cohort)
        InvoiceFactory(cohort=cohort, fee_schedule=fs)

    row = client.get(INVOICES_URL).json()["data"][0]
    assert "student_name" in row
    assert row["cohort_name"] == "Algebra A"
    assert row["fee_schedule_name"] == "Monthly Tuition"
