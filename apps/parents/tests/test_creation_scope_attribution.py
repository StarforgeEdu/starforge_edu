"""Immutable creation-scope regressions for unassigned parent profiles."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django_tenants.utils import schema_context

from core.historical_scope import ScopeAttributionStatus

pytestmark = pytest.mark.django_db


def test_unassigned_parent_cannot_be_claimed_from_another_branch(tenant_a, as_user):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import Guardian, ParentProfile
    from apps.parents.tests.factories import ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        operator_type = AccountType.objects.create(
            name="Scoped family operator",
            slug="scoped-family-operator-creation",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.bulk_create(
            [
                AccountTypePermission(account_type=operator_type, permission="parents:read"),
                AccountTypePermission(account_type=operator_type, permission="parents:write"),
            ]
        )
        actor_a = UserFactory()
        actor_b = UserFactory()
        RoleMembership.objects.create(
            user=actor_a,
            branch=branch_a,
            account_type=operator_type,
            role=operator_type.compatibility_role,
        )
        unrelated_type = AccountType.objects.create(
            name="Unrelated branch role",
            slug="unrelated-parent-creation-role",
            account_kind=AccountType.AccountKind.STAFF,
        )
        RoleMembership.objects.create(
            user=actor_a,
            branch=branch_b,
            account_type=unrelated_type,
            role=unrelated_type.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor_b,
            branch=branch_b,
            account_type=operator_type,
            role=operator_type.compatibility_role,
        )
        student_a = StudentProfileFactory(branch=branch_a)
        student_b = StudentProfileFactory(branch=branch_b)
        legacy_unresolved = ParentProfileFactory()
        actor_a.refresh_from_db()
        actor_b.refresh_from_db()

    client_a = as_user(tenant_a, actor_a)
    denied_create = client_a.post(
        "/api/v1/parents/",
        {
            "phone": "+998905559800",
            "first_name": "Denied",
            "branch": branch_b.pk,
        },
        format="json",
    )
    assert denied_create.status_code == 404
    created = client_a.post(
        "/api/v1/parents/",
        {"phone": "+998905559801", "first_name": "Scoped"},
        format="json",
    )
    assert created.status_code == 201, created.content
    parent_id = created.json()["data"]["id"]

    with schema_context(tenant_a.schema_name):
        parent = ParentProfile.objects.get(pk=parent_id)
        assert not ParentProfile.objects.filter(phone="+998905559800").exists()
        assert parent.branch_at_creation_id == branch_a.pk
        assert parent.department_at_creation_id is None
        assert parent.attribution_status == ScopeAttributionStatus.CAPTURED
        assert parent.created_by_id == actor_a.pk
        missing_parent_id = max(parent_id, legacy_unresolved.pk) + 1000

    # The creation snapshot makes an unassigned row discoverable only inside
    # its exact scope, so the creator can continue a two-step create/link flow.
    assert client_a.get(f"/api/v1/parents/{parent_id}/").status_code == 200
    client_b = as_user(tenant_a, actor_b)
    assert client_b.get(f"/api/v1/parents/{parent_id}/").status_code == 404

    def link(client, candidate_parent_id, student_id):
        return client.post(
            "/api/v1/parents/guardians/",
            {
                "parent": candidate_parent_id,
                "student": student_id,
                "relationship": "legal_guardian",
            },
            format="json",
        )

    remote = link(client_b, parent_id, student_b.pk)
    missing = link(client_b, missing_parent_id, student_b.pk)
    assert remote.status_code == missing.status_code == 400
    assert remote.json()["code"] == missing.json()["code"] == "invalid_parent"
    assert set(remote.json()["errors"]) == set(missing.json()["errors"]) == {"parent"}

    # Legacy rows without unambiguous evidence remain quarantined from scoped
    # linking rather than inheriting whichever branch guesses the id first.
    unresolved = link(client_a, legacy_unresolved.pk, student_a.pk)
    assert unresolved.status_code == 400
    assert unresolved.json()["code"] == "invalid_parent"

    allowed = link(client_a, parent_id, student_a.pk)
    assert allowed.status_code == 201, allowed.content
    assert client_b.get(f"/api/v1/parents/{parent_id}/").status_code == 404
    still_remote = link(client_b, parent_id, student_b.pk)
    assert still_remote.status_code == 400
    assert still_remote.json()["code"] == "invalid_parent"
    with schema_context(tenant_a.schema_name):
        assert Guardian.objects.filter(parent_id=parent_id, student=student_a).exists()
        assert not Guardian.objects.filter(parent_id=parent_id, student=student_b).exists()


def test_parent_creation_scope_is_immutable_after_insert(tenant_a):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.tests.factories import ParentProfileFactory

    with schema_context(tenant_a.schema_name):
        original = BranchFactory()
        replacement = BranchFactory()
        parent = ParentProfileFactory(
            branch_at_creation=original,
            attribution_status=ScopeAttributionStatus.CAPTURED,
        )
        parent.branch_at_creation = replacement
        with pytest.raises(DjangoValidationError):
            parent.save(update_fields=["branch_at_creation"])
