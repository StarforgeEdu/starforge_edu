"""The compensation boundary is independent of faculty and customer finance."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TEACHERS = "/api/v1/teachers/"
POLICY = "/api/v1/teachers/{}/payout-policy/"
PREPARE = "/api/v1/teachers/{}/prepare-salary/"
APPROVALS = "/api/v1/approvals/requests/"


def _grant_user(tenant, *, user_in, branch_permissions):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.users.models import RoleMembership

    with schema_context(tenant.schema_name):
        user = user_in(tenant)
        for index, (branch, permissions) in enumerate(branch_permissions):
            account_type = AccountType.objects.create(
                name=f"Compensation test type {user.pk}-{index}",
                slug=f"compensation-test-{user.pk}-{index}",
                account_kind=AccountType.AccountKind.STAFF,
            )
            AccountTypePermission.objects.bulk_create(
                [
                    AccountTypePermission(account_type=account_type, permission=permission)
                    for permission in permissions
                ]
            )
            RoleMembership.objects.create(
                user=user,
                account_type=account_type,
                role=account_type.compatibility_role,
                branch=branch,
            )
        user.refresh_from_db()
        return user


def test_customer_finance_grant_does_not_reveal_staff_pay(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.services import set_payout_policy
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher = TeacherProfileFactory(branch=branch, rate="9000000")
        set_payout_policy(
            teacher=teacher,
            method="flat_monthly",
            flat_amount_uzs=Decimal("9000000"),
        )
    user = _grant_user(
        tenant_a,
        user_in=user_in,
        branch_permissions=[(branch, {"teachers:read", "finance:*"})],
    )

    client = as_user(tenant_a, user)
    detail = client.get(f"{TEACHERS}{teacher.pk}/")

    assert detail.status_code == 200
    assert "rate" not in detail.json()["data"]
    assert "salary_type" not in detail.json()["data"]
    assert client.get(POLICY.format(teacher.pk)).status_code == 403


def test_compensation_operator_needs_no_faculty_directory_grant(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.services import set_payout_policy
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher = TeacherProfileFactory(branch=branch)
        set_payout_policy(
            teacher=teacher,
            method="flat_monthly",
            flat_amount_uzs=Decimal("3000000"),
        )
    user = _grant_user(
        tenant_a,
        user_in=user_in,
        branch_permissions=[(branch, {"compensation:read"})],
    )
    client = as_user(tenant_a, user)

    assert client.get(TEACHERS).status_code == 403
    policy = client.get(POLICY.format(teacher.pk))
    assert policy.status_code == 200
    assert policy.json()["data"]["flat_amount_uzs"] == "3000000.00"


def test_faculty_writer_cannot_move_hidden_pay_into_another_scope(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.models import TeacherProfile
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        teacher = TeacherProfileFactory(branch=source, rate="8000000")
    user = _grant_user(
        tenant_a,
        user_in=user_in,
        branch_permissions=[
            (source, {"teachers:write"}),
            (target, {"teachers:write"}),
        ],
    )

    response = as_user(tenant_a, user).patch(
        f"{TEACHERS}{teacher.pk}/",
        {"branch": target.pk},
        format="json",
    )

    assert response.status_code == 403
    with schema_context(tenant_a.schema_name):
        assert TeacherProfile.objects.get(pk=teacher.pk).branch_id == source.pk


def test_hod_cannot_discover_or_decide_salary_approvals(
    tenant_a,
    as_role,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.services import set_payout_policy
    from apps.teachers.tests.factories import TeacherProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher = TeacherProfileFactory(branch=branch)
        set_payout_policy(
            teacher=teacher,
            method="flat_monthly",
            flat_amount_uzs=Decimal("2500000"),
        )
    today = timezone.localdate()
    prepared = director.post(
        PREPARE.format(teacher.pk),
        {
            "period_start": (today - timedelta(days=1)).isoformat(),
            "period_end": today.isoformat(),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-hod-privacy-0001",
    )
    assert prepared.status_code == 201, prepared.content
    request_id = prepared.json()["data"]["request_id"]

    hod = as_user(
        tenant_a,
        user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch),
    )
    listed = hod.get(APPROVALS, {"kind": "salary_prep", "page_size": 100})

    assert listed.status_code == 200
    assert listed.json()["pagination"]["total"] == 0
    assert hod.get(f"{APPROVALS}{request_id}/").status_code == 404
    assert hod.post(f"{APPROVALS}{request_id}/approve/", {}, format="json").status_code == 404


def test_cashier_can_disburse_but_cannot_read_or_change_policy(
    tenant_a,
    as_role,
    user_in,
    as_user,
):
    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.services import set_payout_policy
    from apps.teachers.tests.factories import TeacherProfileFactory

    maker, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher = TeacherProfileFactory(branch=branch)
        method = PaymentMethod.objects.create(name="Cash", slug="compensation-cash")
        set_payout_policy(
            teacher=teacher,
            method="flat_monthly",
            flat_amount_uzs=Decimal("2500000"),
        )
    today = timezone.localdate()
    prepared = maker.post(
        PREPARE.format(teacher.pk),
        {"period_start": today.isoformat(), "period_end": today.isoformat()},
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-cashier-flow-0001",
    )
    request_id = prepared.json()["data"]["request_id"]

    approver = as_user(
        tenant_a,
        user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch),
    )
    approved = approver.post(f"{APPROVALS}{request_id}/approve/", {}, format="json")
    assert approved.status_code == 200, approved.content

    cashier = as_user(
        tenant_a,
        user_in(tenant_a, roles=[Role.CASHIER], branch=branch),
    )
    assert cashier.get(POLICY.format(teacher.pk)).status_code == 403
    disbursed = cashier.post(
        f"{APPROVALS}{request_id}/disburse/",
        {"payment_method": method.pk},
        format="json",
    )
    assert disbursed.status_code == 200, disbursed.content
