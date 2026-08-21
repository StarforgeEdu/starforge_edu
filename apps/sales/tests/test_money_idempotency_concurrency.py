"""Database-level concurrency proofs for money-IN retry serialization."""

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


def _actor(tenant, user_in, *, branch):
    user = user_in(tenant, roles=[Role.CASHIER], branch=branch)
    with schema_context(tenant.schema_name):
        principal = ensure_role_principal(user, roles=[Role.CASHIER], branch=branch)
    return user, RolePrincipal(kind="staff", principal_id=principal.pk, user_id=user.pk)


def test_concurrent_identical_sale_retries_create_one_sale_and_ledger_row(tenant_a, user_in):
    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        student = StudentProfileFactory.create(branch=branch)
        method = PaymentMethod.objects.create(
            name="Sale concurrency",
            slug=f"sale-concurrency-{branch.pk}",
        )
    actor, principal = _actor(tenant_a, user_in, branch=branch)
    actor_id = actor.pk
    principal_id = principal.principal_id
    student_id = student.pk
    method_id = method.pk
    barrier = Barrier(2)

    def submit() -> int:
        from apps.sales.services import record_sale
        from apps.students.models import StudentProfile
        from apps.users.models import User

        close_old_connections()
        try:
            with schema_context(tenant_a.schema_name):
                current_actor = User.objects.get(pk=actor_id)
                current_student = StudentProfile.objects.get(pk=student_id)
                barrier.wait(timeout=10)
                result = record_sale(
                    item="Concurrent book",
                    quantity=2,
                    unit_price_uzs=Decimal("50.00"),
                    student=current_student,
                    payment_method_id=method_id,
                    sold_by=current_actor,
                    principal=RolePrincipal(
                        kind="staff",
                        principal_id=principal_id,
                        user_id=actor_id,
                    ),
                    idempotency_key="concurrent-sale-00001",
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
    from apps.sales.models import Sale

    assert len(set(result_ids)) == 1
    with schema_context(tenant_a.schema_name):
        assert Sale.objects.filter(student_id=student_id).count() == 1
        assert (
            LedgerEntry.objects.filter(
                entry_type="book_sale",
                source_kind="sale",
                source_id=result_ids[0],
            ).count()
            == 1
        )


def test_sale_reloads_stale_view_student_under_lock_before_branch_snapshot(tenant_a, user_in):
    """A transfer after the view lookup cannot create money in the old scope."""

    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory
    from apps.sales.models import Sale
    from apps.sales.services import record_sale
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        original_branch = BranchFactory.create()
        transferred_branch = BranchFactory.create()
        stale_student = StudentProfileFactory.create(branch=original_branch)
        method = PaymentMethod.objects.create(
            name="Transfer race",
            slug=f"sale-transfer-race-{original_branch.pk}",
        )
    actor, principal = _actor(tenant_a, user_in, branch=original_branch)
    with schema_context(tenant_a.schema_name):
        # This is the interleaving that used to be vulnerable: the view retained
        # ``stale_student`` while a transfer committed before the service write.
        StudentProfile.objects.filter(pk=stale_student.pk).update(branch=transferred_branch)
        with pytest.raises(NotFoundException) as denied:
            record_sale(
                item="Stale-scope book",
                quantity=1,
                unit_price_uzs=Decimal("100.00"),
                student=stale_student,
                payment_method_id=method.pk,
                sold_by=actor,
                principal=principal,
                idempotency_key="sale-transfer-race-0001",
                is_unscoped=False,
                branch_ids={original_branch.pk},
            )
        assert denied.value.code == "not_found"
        assert not Sale.objects.filter(student_id=stale_student.pk).exists()
