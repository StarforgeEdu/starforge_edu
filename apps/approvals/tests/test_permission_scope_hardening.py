"""Adversarial approval permission-boundary regressions."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db


def test_read_and_handler_grants_combine_only_at_the_same_branch(tenant_a, as_user):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.approvals.models import ApprovalRequest
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        readable_branch = BranchFactory()
        handler_only_branch = BranchFactory()
        reader = AccountType.objects.create(
            name="Scoped approval reader",
            slug="scoped-approval-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        approver = AccountType.objects.create(
            name="Scoped approval handler",
            slug="scoped-approval-handler",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(account_type=reader, permission="approvals:read")
        AccountTypePermission.objects.create(account_type=approver, permission="approvals:approve")
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=readable_branch,
            account_type=reader,
            role=reader.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=readable_branch,
            account_type=approver,
            role=approver.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=handler_only_branch,
            account_type=approver,
            role=approver.compatibility_role,
        )
        local_request = ApprovalRequest.objects.create(
            branch=readable_branch,
            requested_by=UserFactory(),
            kind="expense",
            title="Readable and actionable",
            amount_uzs="100.00",
        )
        remote_request = ApprovalRequest.objects.create(
            branch=handler_only_branch,
            requested_by=UserFactory(),
            kind="expense",
            title="Actionable but not readable",
            amount_uzs="100.00",
        )
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    listing = client.get("/api/v1/approvals/requests/")
    assert listing.status_code == 200, listing.content
    assert {row["id"] for row in listing.json()["data"]} == {local_request.pk}
    assert client.get(f"/api/v1/approvals/requests/{local_request.pk}/").status_code == 200
    assert client.get(f"/api/v1/approvals/requests/{remote_request.pk}/").status_code == 404

    # The action route scopes against approvals:approve itself. It remains
    # usable in Branch B without widening the approvals:read register there.
    approved = client.post(
        f"/api/v1/approvals/requests/{remote_request.pk}/approve/",
        {},
        format="json",
    )
    assert approved.status_code == 200, approved.content
