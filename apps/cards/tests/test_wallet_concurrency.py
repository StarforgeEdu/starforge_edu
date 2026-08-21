from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.db import close_old_connections
from django_tenants.utils import schema_context

from apps.cards import services
from apps.cards.models import Wallet, WalletTransaction
from apps.students.models import StudentProfile
from apps.students.tests.factories import StudentProfileFactory
from apps.users.models import User
from core.permissions import Role
from core.role_principals import RolePrincipal
from tests.role_principal_helpers import ensure_role_principal

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_exact_wallet_retries_mutate_the_balance_once(tenant_a, user_in):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    cashier = user_in(tenant_a, roles=[Role.CASHIER], branch=branch)
    with schema_context(tenant_a.schema_name):
        principal = ensure_role_principal(cashier, roles=[Role.CASHIER], branch=branch)
        student = StudentProfileFactory.create(branch=branch, status=StudentProfile.Status.ACTIVE)
        actor_id = cashier.pk
        principal_id = principal.pk
        student_id = student.pk
        branch_id = branch.pk
    barrier = Barrier(2)

    def submit() -> int:
        close_old_connections()
        try:
            with schema_context(tenant_a.schema_name):
                actor = User.objects.get(pk=actor_id)
                scoped_student = StudentProfile.objects.get(pk=student_id)
                barrier.wait(timeout=10)
                transaction = services.top_up(
                    student=scoped_student,
                    amount=Decimal("17500.00"),
                    actor=actor,
                    principal=RolePrincipal(
                        kind="staff",
                        principal_id=principal_id,
                        user_id=actor_id,
                    ),
                    idempotency_key="concurrent-wallet-retry-0001",
                    is_unscoped=False,
                    branch_ids={branch_id},
                    note="Concurrent retry",
                )
                return transaction.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        transaction_ids = list(pool.map(lambda _index: submit(), range(2)))

    assert len(set(transaction_ids)) == 1
    with schema_context(tenant_a.schema_name):
        assert WalletTransaction.objects.filter(wallet__student_id=student_id).count() == 1
        assert Wallet.objects.get(student_id=student_id).balance_uzs == Decimal("17500.00")
