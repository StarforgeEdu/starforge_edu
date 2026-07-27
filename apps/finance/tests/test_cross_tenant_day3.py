"""D3-F-9 — cross-tenant sweep over every Day-3 FINANCE endpoint (TD-1).

A JWT minted in tenant A, presented on tenant B's host, must 401
``tenant_mismatch`` at the auth gate — BEFORE any object lookup, so arbitrary
path ids are fine. This is the load-bearing isolation invariant (TESTING.md §3
cat 3); a 403/404/422 here would mean the tenant check ran too late and leaked
the resource's existence.

Also asserts tenant-A finance rows are invisible from tenant B even as director.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db

# Every finance endpoint x the verbs it accepts (DAY-3.md Lane A surface).
FINANCE_ENDPOINTS = [
    ("get", "/api/v1/finance/fee-schedules/"),
    ("post", "/api/v1/finance/fee-schedules/"),
    ("get", "/api/v1/finance/invoices/"),
    ("post", "/api/v1/finance/invoices/"),
    ("get", "/api/v1/finance/invoices/1/"),
    ("post", "/api/v1/finance/invoices/1/void/"),
    ("post", "/api/v1/finance/invoices/1/payment-plan/"),
    ("get", "/api/v1/finance/discounts/"),
    ("post", "/api/v1/finance/discounts/"),
    ("get", "/api/v1/finance/outstanding/?student=1"),
    ("post", "/api/v1/finance/cashier-shifts/open/"),
    ("post", "/api/v1/finance/cashier-shifts/1/close/"),
    ("get", "/api/v1/finance/cashier-shifts/1/report/"),
    ("post", "/api/v1/finance/students/1/statement/"),
    ("get", "/api/v1/finance/statements/sometask/"),
]


@pytest.mark.parametrize(("method", "url"), FINANCE_ENDPOINTS)
def test_finance_cross_tenant_token_rejected(tenant_a, tenant_b, user_in, client_for, method, url):
    from apps.auth.services import issue_token

    user = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        access = issue_token(user)["access"]

    client_b = client_for(tenant_b)
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = getattr(client_b, method)(url, {}, format="json")

    assert resp.status_code == 401, (url, resp.status_code, resp.content)
    assert resp.json()["code"] == "authentication_failed"


def test_finance_invoices_invisible_across_tenant(tenant_a, tenant_b, user_in, as_user):
    """tenant-A invoices are not visible from tenant B even with director role."""
    from datetime import date
    from decimal import Decimal

    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.finance.models import Invoice
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=cohort, student=student)
        Invoice.objects.create(
            number="INV-2026-000001",
            student=student,
            cohort=cohort,
            status="issued",
            issue_date=date(2026, 6, 1),
            due_date=date(2026, 6, 30),
            currency="UZS",
            total_uzs=Decimal("150000.00"),
        )

    director_b = user_in(tenant_b, roles=["director"])
    body = as_user(tenant_b, director_b).get("/api/v1/finance/invoices/").json()
    assert body["pagination"]["total"] == 0
