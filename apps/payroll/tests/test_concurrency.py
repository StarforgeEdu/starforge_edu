from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier

import pytest
from django.db import close_old_connections
from django_tenants.utils import schema_context

from apps.payroll.dto import PaymentReconciliationDTO, PreviewFilterDTO
from apps.payroll.models import PayrollLineItem, PayrollReconciliation
from apps.payroll.services import approve_period, reconcile_payment, run_period
from apps.users.models import User
from core.role_principals import RolePrincipal

from .helpers import make_actor, make_period, make_teacher

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_identical_payment_retries_append_one_ledger_movement(tenant_a):
    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        maker = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:run"),
        )
        checker = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:approve"),
        )
        cashier = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:disburse"),
        )
        teacher = make_teacher(branch=branch, amount="900000.00")
        period = make_period(actor=maker, branch=branch)
        period = run_period(
            period=period,
            filters=PreviewFilterDTO((teacher.pk,)),
            actor=maker.user,
            principal=maker.principal,
            idempotency_key="concurrent-run-00001",
        )
        period = approve_period(
            period=period,
            actor=checker.user,
            principal=checker.principal,
            note="Approved",
            idempotency_key="concurrent-approve-1",
        )
        line_id = PayrollLineItem.objects.get(period=period).pk
        method_id = PaymentMethod.objects.create(
            name="Concurrency bank", slug=f"concurrency-bank-{period.pk}"
        ).pk
        actor_id = cashier.user.pk
        principal_id = cashier.principal.principal_id
        period_id = period.pk
        # Both workers must submit byte-for-byte equivalent business input.
        # Generating this inside each thread changes the idempotency
        # fingerprint by microseconds and correctly represents key reuse with
        # a different payload rather than an identical retry.
        paid_at = datetime.now(UTC)
    barrier = Barrier(2)

    def submit() -> int:
        close_old_connections()
        try:
            with schema_context(tenant_a.schema_name):
                actor = User.objects.get(pk=actor_id)
                barrier.wait(timeout=10)
                result = reconcile_payment(
                    period=period.__class__.objects.get(pk=period_id),
                    dto=PaymentReconciliationDTO(
                        line_item_id=line_id,
                        amount_uzs=Decimal("900000.00"),
                        payment_method_id=method_id,
                        external_reference=f"CONCURRENT-PAY-{period_id}",
                        paid_at=paid_at,
                        idempotency_key=f"concurrent-payment-{period_id:08d}",
                    ),
                    actor=actor,
                    principal=RolePrincipal(
                        kind="staff",
                        principal_id=principal_id,
                        user_id=actor_id,
                    ),
                )
                return result.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result_ids = list(pool.map(lambda _index: submit(), range(2)))

    assert len(set(result_ids)) == 1
    with schema_context(tenant_a.schema_name):
        assert PayrollReconciliation.objects.filter(line_item_id=line_id).count() == 1
        assert period.__class__.objects.get(pk=period_id).paid_total_uzs == Decimal("900000.00")
