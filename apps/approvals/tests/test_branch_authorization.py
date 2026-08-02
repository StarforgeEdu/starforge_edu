"""Regression coverage for permission-membership branch isolation."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

REQUESTS = "/api/v1/approvals/requests/"
LEDGER = "/api/v1/approvals/ledger/"


def _branches(tenant):
    with schema_context(tenant.schema_name):
        from apps.org.tests.factories import BranchFactory

        return BranchFactory(), BranchFactory()


def _handler_with_unrelated_branch(
    tenant,
    *,
    role: str,
    allowed_branch,
    unrelated_branch,
    user_in,
    as_user,
    unrelated_role: str = Role.TEACHER,
):
    actor = user_in(tenant, roles=[role], branch=allowed_branch)
    with schema_context(tenant.schema_name):
        from apps.users.models import RoleMembership

        # Teacher can read/write their own approvals, but cannot approve,
        # disburse, or read the ledger.  Its Branch B membership must not borrow
        # the handler permission supplied by the Branch A membership.
        RoleMembership.objects.create(user=actor, branch=unrelated_branch, role=unrelated_role)
        actor.refresh_from_db()
    return as_user(tenant, actor), actor


def _approval_pair(tenant, *, branch_a, branch_b, status):
    with schema_context(tenant.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.users.tests.factories import UserFactory

        requester_a = UserFactory()
        requester_b = UserFactory()
        decider = UserFactory() if status == ApprovalRequest.Status.APPROVED else None
        common = {
            "kind": "expense",
            "amount_uzs": "100.00",
            "status": status,
            "decided_by": decider,
        }
        in_scope = ApprovalRequest.objects.create(
            branch=branch_a,
            requested_by=requester_a,
            title="Branch A",
            **common,
        )
        out_of_scope = ApprovalRequest.objects.create(
            branch=branch_b,
            requested_by=requester_b,
            title="Branch B",
            **common,
        )
        return in_scope, out_of_scope


@pytest.mark.parametrize(
    ("role", "action", "initial_status"),
    [
        (Role.HEAD_OF_DEPT, "approve", "pending"),
        (Role.ACCOUNTANT, "approve", "pending"),
        (Role.CASHIER, "disburse", "approved"),
        (Role.ACCOUNTANT, "disburse", "approved"),
    ],
)
def test_handlers_cannot_list_read_or_act_across_membership_branches(
    tenant_a,
    user_in,
    as_user,
    role,
    action,
    initial_status,
):
    branch_a, branch_b = _branches(tenant_a)
    client, _actor = _handler_with_unrelated_branch(
        tenant_a,
        role=role,
        allowed_branch=branch_a,
        unrelated_branch=branch_b,
        user_in=user_in,
        as_user=as_user,
    )
    in_scope, out_of_scope = _approval_pair(
        tenant_a,
        branch_a=branch_a,
        branch_b=branch_b,
        status=initial_status,
    )

    listed = client.get(REQUESTS)
    assert listed.status_code == 200, listed.content
    assert {item["id"] for item in listed.json()["data"]} == {in_scope.id}
    assert client.get(f"{REQUESTS}{in_scope.id}/").status_code == 200
    assert client.get(f"{REQUESTS}{out_of_scope.id}/").status_code == 404

    payload = {}
    if action == "disburse":
        with schema_context(tenant_a.schema_name):
            from apps.finance.models import PaymentMethod

            payload["payment_method"] = PaymentMethod.objects.create(
                name="Authorization test cash",
                slug=f"auth-cash-{role}",
            ).id

    blocked = client.post(f"{REQUESTS}{out_of_scope.id}/{action}/", payload, format="json")
    assert blocked.status_code == 404, blocked.content

    allowed = client.post(f"{REQUESTS}{in_scope.id}/{action}/", payload, format="json")
    assert allowed.status_code == 200, allowed.content


def test_reject_uses_the_approver_membership_branch(
    tenant_a,
    user_in,
    as_user,
):
    branch_a, branch_b = _branches(tenant_a)
    hod, _ = _handler_with_unrelated_branch(
        tenant_a,
        role=Role.HEAD_OF_DEPT,
        allowed_branch=branch_a,
        unrelated_branch=branch_b,
        user_in=user_in,
        as_user=as_user,
    )
    in_scope, out_of_scope = _approval_pair(
        tenant_a,
        branch_a=branch_a,
        branch_b=branch_b,
        status="pending",
    )

    assert hod.post(f"{REQUESTS}{out_of_scope.id}/reject/", {}, format="json").status_code == 404
    assert hod.post(f"{REQUESTS}{in_scope.id}/reject/", {}, format="json").status_code == 200


def test_cancel_remains_requester_only_without_cross_branch_existence_leak(
    tenant_a,
    user_in,
    as_user,
):
    branch_a, branch_b = _branches(tenant_a)
    hod, actor = _handler_with_unrelated_branch(
        tenant_a,
        role=Role.HEAD_OF_DEPT,
        allowed_branch=branch_a,
        unrelated_branch=branch_b,
        user_in=user_in,
        as_user=as_user,
    )
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.users.tests.factories import UserFactory

        somebody_elses = ApprovalRequest.objects.create(
            branch=branch_b,
            requested_by=UserFactory(),
            kind="expense",
            title="Not mine",
            amount_uzs="1.00",
        )
        own = ApprovalRequest.objects.create(
            branch=branch_a,
            requested_by=actor,
            kind="expense",
            title="Mine",
            amount_uzs="1.00",
        )

    assert hod.post(f"{REQUESTS}{somebody_elses.id}/cancel/", {}, format="json").status_code == 404
    cancelled = hod.post(f"{REQUESTS}{own.id}/cancel/", {}, format="json")
    assert cancelled.status_code == 200, cancelled.content
    assert cancelled.json()["data"]["status"] == "cancelled"


def test_generic_create_asserts_exact_write_branch_and_binds_single_branch(
    tenant_a,
    user_in,
    as_user,
):
    branch_a, branch_b = _branches(tenant_a)
    hod, _ = _handler_with_unrelated_branch(
        tenant_a,
        role=Role.HEAD_OF_DEPT,
        allowed_branch=branch_a,
        unrelated_branch=branch_b,
        user_in=user_in,
        as_user=as_user,
        unrelated_role=Role.LIBRARIAN,
    )
    base = {"kind": "expense", "title": "Scoped expense", "amount_uzs": "10.00"}

    blocked = hod.post(REQUESTS, {**base, "branch": branch_b.id}, format="json")
    assert blocked.status_code == 404, blocked.content
    assert blocked.json()["code"] == "not_found"

    inferred = hod.post(REQUESTS, base, format="json")
    assert inferred.status_code == 201, inferred.content
    assert inferred.json()["data"]["branch"] == branch_a.id


def test_generic_create_requires_branch_when_write_scope_is_ambiguous(
    tenant_a,
    user_in,
    as_user,
):
    branch_a, branch_b = _branches(tenant_a)
    hod_user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    with schema_context(tenant_a.schema_name):
        from apps.users.models import RoleMembership

        RoleMembership.objects.create(user=hod_user, branch=branch_b, role=Role.HEAD_OF_DEPT)
        hod_user.refresh_from_db()
    hod = as_user(tenant_a, hod_user)

    response = hod.post(
        REQUESTS,
        {"kind": "expense", "title": "Ambiguous", "amount_uzs": "10.00"},
        format="json",
    )
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "validation_error"
    assert "branch" in response.json()["errors"]


def test_director_and_superuser_retain_tenant_wide_approval_access(
    tenant_a,
    user_in,
    as_user,
):
    branch_a, branch_b = _branches(tenant_a)
    _in_scope, branch_b_request = _approval_pair(
        tenant_a,
        branch_a=branch_a,
        branch_b=branch_b,
        status="pending",
    )
    director_user = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch_a)
    director = as_user(tenant_a, director_user)
    superuser_user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        superuser_user.is_superuser = True
        superuser_user.save(update_fields=["is_superuser"])
    superuser = as_user(tenant_a, superuser_user)

    assert director.get(f"{REQUESTS}{branch_b_request.id}/").status_code == 200
    assert superuser.get(f"{REQUESTS}{branch_b_request.id}/").status_code == 200

    center_wide = director.post(
        REQUESTS,
        {"kind": "other", "title": "Center-wide"},
        format="json",
    )
    assert center_wide.status_code == 201, center_wide.content
    assert center_wide.json()["data"]["branch"] is None


@pytest.mark.parametrize("role", [Role.CASHIER, Role.ACCOUNTANT])
def test_ledger_list_and_detail_are_exactly_branch_scoped(
    tenant_a,
    user_in,
    as_user,
    role,
):
    branch_a, branch_b = _branches(tenant_a)
    client, _actor = _handler_with_unrelated_branch(
        tenant_a,
        role=role,
        allowed_branch=branch_a,
        unrelated_branch=branch_b,
        user_in=user_in,
        as_user=as_user,
    )
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import LedgerEntry

        in_scope = LedgerEntry.objects.create(
            branch=branch_a,
            direction="in",
            entry_type="scope_test",
            amount_uzs="1.00",
        )
        out_of_scope = LedgerEntry.objects.create(
            branch=branch_b,
            direction="in",
            entry_type="scope_test",
            amount_uzs="2.00",
        )
        center_wide = LedgerEntry.objects.create(
            direction="in",
            entry_type="scope_test",
            amount_uzs="3.00",
        )

    listed = client.get(LEDGER)
    assert listed.status_code == 200, listed.content
    assert {item["id"] for item in listed.json()["data"]} == {in_scope.id}
    assert client.get(f"{LEDGER}{in_scope.id}/").status_code == 200
    assert client.get(f"{LEDGER}{out_of_scope.id}/").status_code == 404
    assert client.get(f"{LEDGER}{center_wide.id}/").status_code == 404


def test_director_and_superuser_retain_tenant_wide_ledger_access(
    tenant_a,
    user_in,
    as_user,
):
    branch_a, branch_b = _branches(tenant_a)
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import LedgerEntry

        entries = [
            LedgerEntry.objects.create(
                branch=branch,
                direction="in",
                entry_type="scope_test",
                amount_uzs="1.00",
            )
            for branch in (branch_a, branch_b, None)
        ]
    director = as_user(tenant_a, user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch_a))
    superuser_user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        superuser_user.is_superuser = True
        superuser_user.save(update_fields=["is_superuser"])
    superuser = as_user(tenant_a, superuser_user)

    expected_ids = {entry.id for entry in entries}
    assert {item["id"] for item in director.get(LEDGER).json()["data"]} == expected_ids
    assert {item["id"] for item in superuser.get(LEDGER).json()["data"]} == expected_ids
    for entry in entries:
        assert director.get(f"{LEDGER}{entry.id}/").status_code == 200
        assert superuser.get(f"{LEDGER}{entry.id}/").status_code == 200
