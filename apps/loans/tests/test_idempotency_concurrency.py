"""Database-level concurrency proof for staff-loan repayment retries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.db import close_old_connections
from django_tenants.utils import schema_context

from core.exceptions import NotFoundException
from core.permissions import Role
from core.role_principals import RolePrincipal
from tests.role_principal_helpers import ensure_role_principal

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_identical_loan_retries_create_one_repayment_and_ledger_row(tenant_a, user_in):
    from apps.approvals.models import ApprovalRequest
    from apps.approvals.services import KIND_LOAN
    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        method = PaymentMethod.objects.create(
            name="Loan concurrency",
            slug=f"loan-concurrency-{branch.pk}",
        )
    actor = user_in(tenant_a, roles=[Role.CASHIER], branch=branch)
    with schema_context(tenant_a.schema_name):
        principal = ensure_role_principal(actor, roles=[Role.CASHIER], branch=branch)
        loan = ApprovalRequest.objects.create(
            kind=KIND_LOAN,
            branch=branch,
            requested_by=actor,
            title="Concurrent loan",
            amount_uzs=Decimal("1000.00"),
            payload={"borrower_id": actor.pk, "party_label": "Concurrent cashier"},
            status=ApprovalRequest.Status.DISBURSED,
        )
    actor_id = actor.pk
    principal_id = principal.pk
    loan_id = loan.pk
    method_id = method.pk
    barrier = Barrier(2)

    def submit() -> int:
        from apps.loans.services import record_repayment
        from apps.users.models import User

        close_old_connections()
        try:
            with schema_context(tenant_a.schema_name):
                current_actor = User.objects.get(pk=actor_id)
                barrier.wait(timeout=10)
                result = record_repayment(
                    loan_id=loan_id,
                    amount_uzs=Decimal("300.00"),
                    payment_method_id=method_id,
                    actor=current_actor,
                    principal=RolePrincipal(
                        kind="staff",
                        principal_id=principal_id,
                        user_id=actor_id,
                    ),
                    idempotency_key="concurrent-loan-repay-01",
                    is_unscoped=False,
                    branch_ids={branch.pk},
                    note="same input",
                )
                return result.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result_ids = list(pool.map(lambda _index: submit(), range(2)))

    from apps.approvals.models import LedgerEntry
    from apps.loans.models import LoanRepayment

    assert len(set(result_ids)) == 1
    with schema_context(tenant_a.schema_name):
        assert LoanRepayment.objects.filter(loan_id=loan_id).count() == 1
        assert LedgerEntry.objects.filter(
            entry_type="loan_repayment",
            source_kind="approval_request",
            source_id=loan_id,
        ).count() == 1


def test_loan_repayment_rechecks_stale_view_scope_against_locked_row(tenant_a, user_in):
    """A branch change after view lookup cannot receive money in the old scope."""

    from apps.approvals.models import ApprovalRequest, LedgerEntry
    from apps.approvals.services import KIND_LOAN
    from apps.finance.models import PaymentMethod
    from apps.loans.models import LoanRepayment
    from apps.loans.services import record_repayment
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        original_branch = BranchFactory.create()
        moved_branch = BranchFactory.create()
        method = PaymentMethod.objects.create(
            name="Loan move race",
            slug=f"loan-move-race-{original_branch.pk}",
        )
    actor = user_in(tenant_a, roles=[Role.CASHIER], branch=original_branch)
    with schema_context(tenant_a.schema_name):
        principal_profile = ensure_role_principal(
            actor,
            roles=[Role.CASHIER],
            branch=original_branch,
        )
        loan = ApprovalRequest.objects.create(
            kind=KIND_LOAN,
            branch=original_branch,
            requested_by=actor,
            title="Moved loan",
            amount_uzs=Decimal("1000.00"),
            payload={"borrower_id": actor.pk},
            status=ApprovalRequest.Status.DISBURSED,
        )
        # Simulate a branch reassignment committed after _get_visible returned the
        # old object but before the atomic repayment service acquired its lock.
        ApprovalRequest.objects.filter(pk=loan.pk).update(branch=moved_branch)
        with pytest.raises(NotFoundException) as denied:
            record_repayment(
                loan_id=loan.pk,
                amount_uzs=Decimal("100.00"),
                payment_method_id=method.pk,
                actor=actor,
                principal=RolePrincipal(
                    kind="staff",
                    principal_id=principal_profile.pk,
                    user_id=actor.pk,
                ),
                idempotency_key="loan-move-race-00001",
                is_unscoped=False,
                branch_ids={original_branch.pk},
            )
        assert denied.value.code == "not_found"
        assert not LoanRepayment.objects.filter(loan_id=loan.pk).exists()
        assert not LedgerEntry.objects.filter(
            entry_type="loan_repayment",
            source_kind="approval_request",
            source_id=loan.pk,
        ).exists()
