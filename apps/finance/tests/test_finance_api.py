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


def _attach_staff_principal(tenant, user, *, label: str):
    """Give a legacy test session the one exact active role account it may resolve."""
    from apps.org.models import StaffProfile

    with schema_context(tenant.schema_name):
        return StaffProfile.objects.create(
            user=user,
            username=f"finance-{label}-{user.pk}",
        )


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
    assert denied.status_code == 404
    assert denied.json()["code"] == "not_found"


def test_own_finance_grant_cannot_borrow_parent_identity_from_another_membership(
    tenant_a,
    user_in,
    as_user,
):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        actor = user_in(tenant_a)
        parent = ParentProfileFactory(user=actor)
        child = StudentProfileFactory()
        GuardianFactory(parent=parent, student=child)
        InvoiceFactory(student=child, total_uzs=Decimal("100000.00"))

        staff_finance_type = AccountType.objects.create(
            name="Self finance helper",
            slug="self-finance-helper",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=staff_finance_type,
            permission="finance:read_own",
        )
        parent_without_finance_type = AccountType.objects.create(
            name="Parent without finance",
            slug="parent-without-finance",
            account_kind=AccountType.AccountKind.PARENT,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=child.branch,
            account_type=staff_finance_type,
            role=staff_finance_type.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=child.branch,
            account_type=parent_without_finance_type,
            role=parent_without_finance_type.compatibility_role,
        )
        actor.refresh_from_db()

    response = as_user(tenant_a, actor).get(f"{OUTSTANDING_URL}?student={child.pk}")
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


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


def test_create_invoice_endpoint(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        fs = FeeScheduleFactory(amount_uzs=Decimal("777000.00"))
    accountant = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=student.branch)
    client = as_user(tenant_a, accountant)
    resp = client.post(INVOICES_URL, {"student": student.pk, "fee_schedule": fs.pk}, format="json")
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["status"] == "issued"
    assert Decimal(body["total_uzs"]) == Decimal("777000.00")
    assert len(body["lines"]) == 1


def test_invoice_line_explicit_zero_quantity_is_not_coerced_to_one(tenant_a, user_in, as_user):
    """An explicit quantity of 0 must bill 0 (a waived line), not be defaulted to 1
    (the default applies only when the key is absent) — no money over-charge."""
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    accountant = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=student.branch)
    client = as_user(tenant_a, accountant)
    resp = client.post(
        INVOICES_URL,
        {
            "student": student.pk,
            "lines": [{"description": "waived", "unit_price_uzs": "100000", "quantity": 0}],
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()["data"]
    assert Decimal(body["lines"][0]["amount_uzs"]) == Decimal("0.00")
    assert Decimal(body["total_uzs"]) == Decimal("0.00")


def test_invoice_line_oversized_quantity_is_400_not_500(tenant_a, user_in, as_user):
    """A quantity beyond the column's 8 digits is a clean 400, not a decimal-context
    overflow -> 500 in the line-amount quantize."""
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    accountant = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=student.branch)
    client = as_user(tenant_a, accountant)
    resp = client.post(
        INVOICES_URL,
        {
            "student": student.pk,
            "lines": [
                {"description": "x", "unit_price_uzs": "9999999999999999", "quantity": "9999999999999999"}
            ],
        },
        format="json",
    )
    assert resp.status_code == 400, resp.content


def test_invoice_line_valid_fields_with_overflowing_product_is_400(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    actor = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=student.branch)

    response = as_user(tenant_a, actor).post(
        INVOICES_URL,
        {
            "student": student.pk,
            "lines": [
                {
                    "description": "individually valid but overflowing",
                    "unit_price_uzs": "9999999999999999.99",
                    "quantity": "999999.99",
                }
            ],
        },
        format="json",
    )

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "invoice_amount_too_large"


def test_invoice_line_count_is_bounded(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    actor = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=student.branch)

    response = as_user(tenant_a, actor).post(
        INVOICES_URL,
        {
            "student": student.pk,
            "lines": [{"description": f"line-{index}", "unit_price_uzs": "1.00"} for index in range(501)],
        },
        format="json",
    )

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "validation_error"


def test_create_invoice_validation_empty(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
    accountant = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=student.branch)
    client = as_user(tenant_a, accountant)
    resp = client.post(INVOICES_URL, {"student": student.pk}, format="json")
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "line",
    [
        {"description": "bad price", "unit_price_uzs": "-1.00", "quantity": "1"},
        {
            "description": "bad quantity",
            "line_type": "discount",
            "unit_price_uzs": "-1.00",
            "quantity": "-1",
        },
    ],
)
def test_invoice_negative_line_values_are_field_validation_errors(
    tenant_a,
    user_in,
    as_user,
    line,
):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        branch = student.branch
    accountant = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch)
    response = as_user(tenant_a, accountant).post(
        INVOICES_URL,
        {"student": student.pk, "lines": [line]},
        format="json",
    )
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "validation_error"


def test_accountant_invoice_and_statement_access_is_branch_scoped(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory

    with schema_context(tenant_a.schema_name):
        own_student = StudentProfileFactory()
        other_student = StudentProfileFactory()
        own_invoice = InvoiceFactory(student=own_student)
        other_invoice = InvoiceFactory(student=other_student)
        other_schedule = FeeScheduleFactory(
            cohort=CohortFactory(branch=other_student.branch),
        )
        own_branch = own_student.branch
    accountant = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=own_branch)
    _attach_staff_principal(tenant_a, accountant, label="scoped-statement")
    client = as_user(tenant_a, accountant)

    listing = client.get(INVOICES_URL)
    assert listing.status_code == 200
    assert {row["id"] for row in listing.json()["data"]} == {own_invoice.pk}
    assert client.get(f"{INVOICES_URL}{other_invoice.pk}/").status_code == 404
    assert (
        client.post(
            INVOICES_URL,
            {
                "student": other_student.pk,
                "lines": [{"description": "x", "unit_price_uzs": "1.00"}],
            },
            format="json",
        ).status_code
        == 403
    )
    assert (
        client.post(
            INVOICES_URL,
            {"student": own_student.pk, "fee_schedule": other_schedule.pk},
            format="json",
        ).status_code
        == 403
    )
    statement = client.post(f"/api/v1/finance/students/{other_student.pk}/statement/")
    # Scoped reads conceal records outside the caller's branch just like invoice
    # detail; do not turn the statement endpoint into a student-id oracle.
    assert statement.status_code == 404
    assert statement.json()["code"] == "not_found"


def test_outstanding_total_excludes_historical_invoices_outside_staff_scope(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        historical_branch = BranchFactory(name="Historical balance", slug="historical-balance")
        current_branch = BranchFactory(name="Current balance", slug="current-balance")
        student = StudentProfileFactory(branch=current_branch, current_cohort=None)
        InvoiceFactory(
            student=student,
            branch_at_issue=historical_branch,
            department_at_issue=None,
            total_uzs=Decimal("100.00"),
        )
        visible = InvoiceFactory(
            student=student,
            branch_at_issue=current_branch,
            department_at_issue=None,
            total_uzs=Decimal("200.00"),
        )
    actor = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=current_branch)

    response = as_user(tenant_a, actor).get(f"{OUTSTANDING_URL}?student={student.pk}")

    assert response.status_code == 200, response.content
    assert response.json()["data"]["outstanding_uzs"] == "200.00"
    assert [row["id"] for row in response.json()["data"]["invoices"]] == [visible.pk]


def test_outstanding_conceals_student_outside_staff_scope_without_historical_invoice(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        local_branch = BranchFactory(name="Local balance", slug="local-balance")
        remote_branch = BranchFactory(name="Remote balance", slug="remote-balance")
        remote_student = StudentProfileFactory(branch=remote_branch, current_cohort=None)
    actor = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=local_branch)

    response = as_user(tenant_a, actor).get(f"{OUTSTANDING_URL}?student={remote_student.pk}")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_outstanding_returns_not_found_for_missing_student(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)

    response = client.get(f"{OUTSTANDING_URL}?student=2147483647")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_local_finance_grant_cannot_borrow_stale_owner_membership_scope(
    tenant_a,
    user_in,
    as_user,
):
    """An unrelated owner-shaped assignment cannot globalize another grant.

    This models an incomplete legacy import where the denormalized ``role``
    column still says director but the canonical custom account type grants
    only finance:write. Read authorization comes only from a separate
    branch-local account type, so the stale role cannot lend organization
    scope to the read operation. The protected system owner itself is immutable
    and must never be weakened to manufacture this fixture.
    """
    from apps.access.models import AccountType, AccountTypePermission
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        local_branch = BranchFactory(name="Local Finance", slug="local-finance")
        remote_branch = BranchFactory(name="Remote Finance", slug="remote-finance")
        local_type = AccountType.objects.create(
            name="Local fee reader",
            slug="local-fee-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=local_type,
            permission="finance:read",
        )
        stale_owner_type = AccountType.objects.create(
            name="Imported finance writer",
            slug="imported-finance-writer",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=stale_owner_type,
            permission="finance:write",
        )
        actor = user_in(tenant_a)
        RoleMembership.objects.create(
            user=actor,
            branch=local_branch,
            account_type=local_type,
            role=local_type.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=remote_branch,
            account_type=stale_owner_type,
            role=Role.DIRECTOR,
        )
        local_schedule = FeeScheduleFactory(
            name="Local schedule",
            cohort=CohortFactory(branch=local_branch),
        )
        remote_schedule = FeeScheduleFactory(
            name="Remote schedule",
            cohort=CohortFactory(branch=remote_branch),
        )
        local_invoice = InvoiceFactory(
            number="INV-LOCAL-GRANT-SCOPE",
            student=StudentProfileFactory(branch=local_branch),
        )
        remote_invoice = InvoiceFactory(
            number="INV-REMOTE-STALE-OWNER",
            student=StudentProfileFactory(branch=remote_branch),
        )
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    fee_response = client.get(FEE_URL)
    assert fee_response.status_code == 200, fee_response.content
    assert {row["id"] for row in fee_response.json()["data"]} == {local_schedule.pk}
    assert remote_schedule.pk not in {row["id"] for row in fee_response.json()["data"]}

    invoice_response = client.get(INVOICES_URL)
    assert invoice_response.status_code == 200, invoice_response.content
    assert {row["id"] for row in invoice_response.json()["data"]} == {local_invoice.pk}
    assert remote_invoice.pk not in {row["id"] for row in invoice_response.json()["data"]}


def test_selector_default_roles_exclude_inactive_account_types(tenant_a, user_in):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.finance import selectors
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(number="INV-INACTIVE-ACCOUNT-TYPE")
        actor = user_in(tenant_a)
        account_type = AccountType.objects.create(
            name="Temporary finance reader",
            slug="temporary-finance-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=account_type,
            permission="finance:read",
        )
        RoleMembership.objects.create(
            user=actor,
            branch=invoice.branch_at_issue,
            account_type=account_type,
            role=account_type.compatibility_role,
        )
        account_type.is_active = False
        account_type.save(update_fields={"is_active"})
        actor.refresh_from_db()

        visible = selectors.scoped_invoice_summaries(user=actor)
        assert not visible.filter(pk=invoice.pk).exists()


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


def test_branch_scoped_finance_writer_cannot_mutate_tenant_global_configuration(
    tenant_a,
    user_in,
    as_user,
):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Scoped finance", slug="scoped-finance")
        cohort = CohortFactory(branch=branch)
    actor = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch)
    client = as_user(tenant_a, actor)

    global_fee = client.post(
        FEE_URL,
        {"name": "Global fee", "amount_uzs": "100.00", "billing_period": "monthly"},
        format="json",
    )
    assert global_fee.status_code == 403
    assert global_fee.json()["code"] == "out_of_scope"
    global_method = client.post(
        "/api/v1/finance/payment-methods/",
        {"name": "Global method"},
        format="json",
    )
    assert global_method.status_code == 403
    assert global_method.json()["code"] == "out_of_scope"

    scoped_fee = client.post(
        FEE_URL,
        {
            "name": "Scoped fee",
            "amount_uzs": "100.00",
            "billing_period": "monthly",
            "cohort": cohort.pk,
        },
        format="json",
    )
    assert scoped_fee.status_code == 201, scoped_fee.content


def test_fee_schedule_authorized_detail_crud(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)
    created_response = client.post(
        FEE_URL,
        {
            "name": "Monthly",
            "amount_uzs": "100000.00",
            "billing_period": "monthly",
            "due_day_of_month": 5,
        },
        format="json",
    )
    assert created_response.status_code == 201, created_response.content
    pk = created_response.json()["data"]["id"]
    assert client.get(f"{FEE_URL}{pk}/").status_code == 200
    patched = client.patch(f"{FEE_URL}{pk}/", {"amount_uzs": "120000.00"}, format="json")
    assert patched.status_code == 200
    assert patched.json()["data"]["amount_uzs"] == "120000.00"
    replaced = client.put(
        f"{FEE_URL}{pk}/",
        {"name": "Monthly renamed", "amount_uzs": "130000.00"},
        format="json",
    )
    assert replaced.status_code == 200
    assert replaced.json()["data"]["name"] == "Monthly renamed"
    assert client.delete(f"{FEE_URL}{pk}/").status_code == 204


def test_payment_plan_http_validates_dates_and_positive_amounts(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(total_uzs=Decimal("100.00"))
    endpoint = f"{INVOICES_URL}{invoice.pk}/payment-plan/"
    negative = client.post(
        endpoint,
        {
            "installments": [
                {"due_date": "2026-08-01", "amount_uzs": "110.00"},
                {"due_date": "2026-09-01", "amount_uzs": "-10.00"},
            ]
        },
        format="json",
    )
    assert negative.status_code == 400, negative.content
    assert (
        client.post(
            endpoint,
            {"installments": [{"due_date": "not-a-date", "amount_uzs": "100.00"}]},
            format="json",
        ).status_code
        == 400
    )
    created_plan = client.post(
        endpoint,
        {
            "installments": [
                {"due_date": "2026-08-01", "amount_uzs": "40.00"},
                {"due_date": "2026-09-01", "amount_uzs": "60.00"},
            ]
        },
        format="json",
    )
    assert created_plan.status_code == 201, created_plan.content
    assert [row["amount_uzs"] for row in created_plan.json()["data"]["installments"]] == [
        "40.00",
        "60.00",
    ]


def test_payment_method_unicode_slug_and_authorized_detail_crud(tenant_a, as_role):
    client, _ = as_role(Role.DIRECTOR)
    endpoint = "/api/v1/finance/payment-methods/"
    first = client.post(endpoint, {"name": "Нақд пул"}, format="json")
    second = client.post(endpoint, {"name": "Карта"}, format="json")
    assert first.status_code == 201, first.content
    assert second.status_code == 201, second.content
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert first_data["slug"]
    assert second_data["slug"]
    assert first_data["slug"] != second_data["slug"]

    pk = first_data["id"]
    renamed = client.patch(f"{endpoint}{pk}/", {"name": "Нақд"}, format="json")
    assert renamed.status_code == 200
    assert renamed.json()["data"]["slug"] == first_data["slug"]
    assert client.get(f"{endpoint}{pk}/").status_code == 200
    assert client.get(endpoint).status_code == 200
    assert client.patch(f"{endpoint}{pk}/", {"slug": "has spaces"}, format="json").status_code == 400
    duplicate = client.patch(
        f"{endpoint}{pk}/",
        {"slug": second_data["slug"]},
        format="json",
    )
    assert duplicate.status_code == 400, duplicate.content
    assert duplicate.json()["code"] == "duplicate_slug"
    assert client.delete(f"{endpoint}{pk}/").status_code == 204


# --------------------------------------------------------------------------- #
# cashier shift endpoints
# --------------------------------------------------------------------------- #


def test_cashier_shift_open_close_endpoints(tenant_a, user_in, as_user):
    cashier = user_in(tenant_a, roles=[Role.CASHIER])
    with schema_context(tenant_a.schema_name):
        branch = cashier.role_memberships.get(role=Role.CASHIER).branch
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


def test_cashier_can_only_read_and_close_own_shift(tenant_a, user_in, as_user):
    from apps.finance import services
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    first = user_in(tenant_a, roles=[Role.CASHIER], branch=branch)
    second = user_in(tenant_a, roles=[Role.CASHIER], branch=branch)
    with schema_context(tenant_a.schema_name):
        own = services.open_cashier_shift(cashier=first, branch=branch)
        other = services.open_cashier_shift(cashier=second, branch=branch)

    client = as_user(tenant_a, first)
    listing = client.get("/api/v1/finance/cashier-shifts/")
    assert listing.status_code == 200
    assert [row["id"] for row in listing.json()["data"]] == [own.pk]
    assert client.get(f"/api/v1/finance/cashier-shifts/{other.pk}/").status_code == 404
    assert (
        client.post(
            f"/api/v1/finance/cashier-shifts/{other.pk}/close/",
            {"closing_cash_uzs": "0.00"},
            format="json",
        ).status_code
        == 404
    )


def test_cashier_shift_me_is_current_operator_only_for_mixed_finance_roles(tenant_a, user_in, as_user):
    """A finance register can contain other cashiers; the till self route cannot."""
    from apps.finance import services
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Till branch", slug="till-branch")
    actor = user_in(tenant_a, roles=[Role.CASHIER], branch=branch)
    other = user_in(tenant_a, roles=[Role.CASHIER], branch=branch)
    with schema_context(tenant_a.schema_name):
        # A second accounting membership makes the general register broad for
        # this actor, which is exactly why a POS must use the self endpoint.
        RoleMembership.objects.create(user=actor, branch=branch, role=Role.ACCOUNTANT)
        own_shift = services.open_cashier_shift(cashier=actor, branch=branch)
        other_shift = services.open_cashier_shift(cashier=other, branch=branch)

    client = as_user(tenant_a, actor)
    broad = client.get("/api/v1/finance/cashier-shifts/")
    assert broad.status_code == 200, broad.content
    assert {row["id"] for row in broad.json()["data"]} == {own_shift.pk, other_shift.pk}

    mine = client.get("/api/v1/finance/cashier-shifts/me/?status=open")
    assert mine.status_code == 200, mine.content
    assert [row["id"] for row in mine.json()["data"]] == [own_shift.pk]


def test_cashier_scope_cannot_borrow_remote_accountant_identity(tenant_a, user_in, as_user):
    from apps.finance import services
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        local_branch = BranchFactory(name="Local till", slug="local-till")
        remote_branch = BranchFactory(name="Remote accounts", slug="remote-accounts")
    actor = user_in(tenant_a, roles=[Role.CASHIER], branch=local_branch)
    local_other = user_in(tenant_a, roles=[Role.CASHIER], branch=local_branch)
    remote_other = user_in(tenant_a, roles=[Role.CASHIER], branch=remote_branch)
    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.create(
            user=actor,
            branch=remote_branch,
            role=Role.ACCOUNTANT,
        )
        actor.refresh_from_db()
        local_own_shift = services.open_cashier_shift(cashier=actor, branch=local_branch)
        local_other_shift = services.open_cashier_shift(cashier=local_other, branch=local_branch)
        remote_other_shift = services.open_cashier_shift(cashier=remote_other, branch=remote_branch)

    client = as_user(tenant_a, actor)
    listing = client.get("/api/v1/finance/cashier-shifts/")
    assert listing.status_code == 200, listing.content
    visible_ids = {row["id"] for row in listing.json()["data"]}
    assert visible_ids == {local_own_shift.pk, remote_other_shift.pk}
    assert local_other_shift.pk not in visible_ids
    assert client.get(f"/api/v1/finance/cashier-shifts/{local_other_shift.pk}/").status_code == 404
    assert client.get(f"/api/v1/finance/cashier-shifts/{remote_other_shift.pk}/").status_code == 200


# --------------------------------------------------------------------------- #
# statement async (202 + result)
# --------------------------------------------------------------------------- #


def test_statement_request_returns_202(tenant_a, user_in, as_user, monkeypatch):
    from celery_tasks.finance_tasks import generate_statement_pdf

    queued = []
    monkeypatch.setattr(
        generate_statement_pdf,
        "delay",
        lambda *args, **kwargs: queued.append((args, kwargs)),
    )
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        InvoiceFactory(student=student)
    accountant = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=student.branch)
    _attach_staff_principal(tenant_a, accountant, label="statement-request")
    client = as_user(tenant_a, accountant)
    resp = client.post(f"/api/v1/finance/students/{student.pk}/statement/", {"locale": "en"}, format="json")
    assert resp.status_code == 202
    export_id = resp.json()["data"]["export_id"]
    assert resp.json()["data"]["task_id"] == export_id
    assert queued == [((export_id,), {"_schema_name": tenant_a.schema_name})]


def test_statement_request_rejects_missing_student_before_enqueue(tenant_a, as_role, monkeypatch):
    from celery_tasks.finance_tasks import generate_statement_pdf

    called = False

    def should_not_enqueue(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("missing students must not reach Celery")

    monkeypatch.setattr(generate_statement_pdf, "delay", should_not_enqueue)
    client, actor = as_role(Role.DIRECTOR)
    _attach_staff_principal(tenant_a, actor, label="missing-student")
    response = client.post("/api/v1/finance/students/999999999/statement/")
    assert response.status_code == 404
    assert response.json()["code"] == "student_not_found"
    assert called is False


def test_statement_result_authorized_done_path(tenant_a, as_role, monkeypatch):
    from django.utils import timezone

    from apps.finance import services as finance_services
    from apps.finance.models import StatementExport, StatementExportInvoice

    client, actor = as_role(Role.DIRECTOR)
    principal = _attach_staff_principal(tenant_a, actor, label="statement-result")
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory()
        now = timezone.now()
        export = StatementExport.objects.create(
            student=invoice.student,
            requested_by=actor,
            requested_by_id_snapshot=actor.pk,
            requested_principal_kind="staff",
            requested_principal_id=principal.pk,
            locale="en",
            invoice_set_hash=finance_services._statement_invoice_set_hash([invoice.pk]),
        )
        StatementExportInvoice.objects.create(export=export, invoice=invoice)
        StatementExport.objects.filter(pk=export.pk).update(
            status=StatementExport.Status.DONE,
            attempt_count=1,
            file_bytes=128,
            started_at=now,
            finished_at=now,
        )
        export.refresh_from_db()
        key = finance_services.expected_statement_export_key(export)
    monkeypatch.setattr(
        "infrastructure.storage.s3_client.presign_download",
        lambda key, **kwargs: f"signed:{key}:{kwargs['expires_in']}",
    )
    response = client.get(f"/api/v1/finance/statements/{export.pk}/")
    assert response.status_code == 200
    assert response.json()["data"]["export_id"] == str(export.pk)
    assert response.json()["data"]["status"] == "done"
    assert response.json()["data"]["url"] == f"signed:{key}:600"


def test_statement_result_rechecks_every_artifact_invoice_scope(
    tenant_a,
    user_in,
    as_user,
    monkeypatch,
):
    from django.core.cache import cache

    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        historical_branch = BranchFactory(name="Historical statement", slug="historical-statement")
        current_branch = BranchFactory(name="Current statement", slug="current-statement")
        student = StudentProfileFactory(branch=current_branch)
        historical_invoice = InvoiceFactory(
            student=student,
            branch_at_issue=historical_branch,
        )
        current_invoice = InvoiceFactory(
            student=student,
            branch_at_issue=current_branch,
        )
    actor = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=current_branch)
    key = f"{tenant_a.schema_name}/documents/statement_{student.pk}_20260802112233_{'b' * 32}.pdf"
    cache.set(
        f"finance:statement:{tenant_a.schema_name}:task-stale-scope",
        {
            "key": key,
            "requested_by_id": actor.pk,
            "student_id": student.pk,
            "invoice_ids": sorted([historical_invoice.pk, current_invoice.pk]),
        },
    )
    monkeypatch.setattr(
        "infrastructure.storage.s3_client.presign_download",
        lambda *_args, **_kwargs: pytest.fail("an artifact outside current scope must never be signed"),
    )

    response = as_user(tenant_a, actor).get("/api/v1/finance/statements/task-stale-scope/")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_statement_result_rejects_cross_tenant_or_legacy_cache_key(tenant_a, as_role, monkeypatch):
    from django.core.cache import cache

    client, actor = as_role(Role.DIRECTOR)
    cache.set(
        f"finance:statement:{tenant_a.schema_name}:task-untrusted",
        {
            "key": "another_tenant/documents/statement_17_20260802112233.pdf",
            "requested_by_id": actor.pk,
            "student_id": 17,
        },
    )
    monkeypatch.setattr(
        "infrastructure.storage.s3_client.presign_download",
        lambda *_args, **_kwargs: pytest.fail("an untrusted statement key must never be signed"),
    )

    response = client.get("/api/v1/finance/statements/task-untrusted/")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# --------------------------------------------------------------------------- #
# list shape + query budget (<=5 per spec)
# --------------------------------------------------------------------------- #


def test_invoice_list_query_budget(as_role, tenant_a, django_assert_max_num_queries):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        for _ in range(20):
            InvoiceFactory(student=student, total_uzs=Decimal("100000.00"))
    # The register is a scalar summary query: no line/allocation prefetches. +1 for
    # billing paywall and +1 for the per-request permission-override load.
    with django_assert_max_num_queries(8):
        body = client.get(INVOICES_URL).json()
    assert set(body) == {"success", "data", "pagination"}
    assert all("lines" not in row and "allocations" not in row for row in body["data"])


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


def test_invoice_list_is_lightweight_and_reports_exact_outstanding_while_detail_is_full(
    tenant_a,
    as_role,
):
    from apps.finance.models import Invoice, InvoiceLine, PaymentAllocation

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(
            status=Invoice.Status.PARTIALLY_PAID,
            total_uzs=Decimal("150000.00"),
        )
        InvoiceLine.objects.create(
            invoice=invoice,
            description="Tuition",
            line_type=InvoiceLine.LineType.TUITION,
            quantity=Decimal("1.00"),
            unit_price_uzs=Decimal("150000.00"),
            amount_uzs=Decimal("150000.00"),
        )
        PaymentAllocation.objects.create(
            invoice=invoice,
            payment_id=991001,
            amount_uzs=Decimal("40000.00"),
        )

    register_row = next(
        row for row in client.get(INVOICES_URL, {"page_size": 100}).json()["data"] if row["id"] == invoice.pk
    )
    assert register_row["total_uzs"] == "150000.00"
    assert register_row["outstanding_uzs"] == "110000.00"
    assert "lines" not in register_row
    assert "allocations" not in register_row

    detail = client.get(f"{INVOICES_URL}{invoice.pk}/").json()["data"]
    assert detail["outstanding_uzs"] == "110000.00"
    assert len(detail["lines"]) == 1
    assert len(detail["allocations"]) == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("draft", "0.00"),
        ("void", "0.00"),
        ("paid", "0.00"),
        ("issued", "150000.00"),
        ("overdue", "150000.00"),
    ],
)
def test_invoice_list_outstanding_respects_receivable_status(tenant_a, as_role, status, expected):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(status=status, total_uzs=Decimal("150000.00"))

    row = next(
        item
        for item in client.get(INVOICES_URL, {"page_size": 100}).json()["data"]
        if item["id"] == invoice.pk
    )
    assert row["outstanding_uzs"] == expected
