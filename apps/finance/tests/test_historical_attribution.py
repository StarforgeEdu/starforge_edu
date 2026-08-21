from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import CommandError, call_command
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.cohorts.tests.factories import CohortFactory
from apps.finance import services as finance_services
from apps.finance.models import CashierShift, Invoice, PaymentAllocation, Refund
from apps.org.tests.factories import BranchFactory, DepartmentFactory
from apps.payments import selectors as payment_selectors
from apps.payments import services as payment_services
from apps.payments.models import Payment
from apps.students.models import StudentProfile
from apps.students.tests.factories import StudentProfileFactory
from apps.users.tests.factories import UserFactory
from core.exceptions import ConflictException, ValidationException
from core.historical_scope import ScopeAttributionStatus
from core.permissions import Role

pytestmark = pytest.mark.django_db


def _legacy_invoice(*, number: str, student, cohort=None, amount: str = "100.00") -> Invoice:
    return Invoice.objects.create(
        number=number,
        student=student,
        cohort=cohort,
        status=Invoice.Status.ISSUED,
        issue_date=date(2026, 7, 1),
        due_date=date(2026, 7, 31),
        currency="UZS",
        total_uzs=Decimal(amount),
    )


def test_issue_and_payment_scope_survive_student_transfer(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory(slug="historical-a")
        branch_b = BranchFactory(slug="historical-b")
        department_a = DepartmentFactory(branch=branch_a, slug="historical-dept-a")
        department_b = DepartmentFactory(branch=branch_b, slug="historical-dept-b")
        cohort_a = CohortFactory(branch=branch_a, department=department_a)
        cohort_b = CohortFactory(branch=branch_b, department=department_b)
        student = StudentProfileFactory(branch=branch_a, current_cohort=cohort_a)

        invoice = finance_services.issue_invoice(
            student_id=student.pk,
            lines=[
                {
                    "description": "Tuition",
                    "line_type": "tuition",
                    "quantity": "1",
                    "unit_price_uzs": "100.00",
                }
            ],
        )
        assert invoice.branch_at_issue_id == branch_a.pk
        assert invoice.department_at_issue_id == department_a.pk
        assert invoice.attribution_status == ScopeAttributionStatus.CAPTURED

        payment, created = payment_services.get_or_create_payment(
            idempotency_key="historical-transfer-payment",
            provider=Payment.Method.CLICK,
            amount_uzs=invoice.total_uzs,
            account_ref=invoice.number,
            metadata={"invoice_id": invoice.pk, "student_id": student.pk},
            invoice=invoice,
        )
        assert created is True
        Payment.objects.filter(pk=payment.pk).update(
            status=Payment.Status.COMPLETED,
            provider_txn_id="historical-transfer-provider",
            paid_at=timezone.now(),
        )
        payment.refresh_from_db()
        assert payment.branch_at_payment_id == branch_a.pk
        assert payment.department_at_payment_id == department_a.pk

        StudentProfile.objects.filter(pk=student.pk).update(
            branch=branch_b,
            current_cohort=cohort_b,
        )

    accountant_a = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch_a)
    accountant_b = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch_b)
    invoices_a = as_user(tenant_a, accountant_a).get("/api/v1/finance/invoices/")
    invoices_b = as_user(tenant_a, accountant_b).get("/api/v1/finance/invoices/")
    assert {row["id"] for row in invoices_a.json()["data"]} == {invoice.pk}
    assert invoices_b.json()["data"] == []

    payments_a = as_user(tenant_a, accountant_a).get("/api/v1/payments/")
    payments_b = as_user(tenant_a, accountant_b).get("/api/v1/payments/")
    assert {row["id"] for row in payments_a.json()["data"]} == {payment.pk}
    assert payments_b.json()["data"] == []

    with schema_context(tenant_a.schema_name):
        payment_day = timezone.localdate(payment.paid_at)
        report_a = payment_selectors.reconciliation(
            on=payment_day,
            scope_pairs={(branch_a.pk, None)},
        )
        report_b = payment_selectors.reconciliation(
            on=payment_day,
            scope_pairs={(branch_b.pk, None)},
        )
    assert report_a["total_paid_uzs"] == "100.00"
    assert report_b["total_paid_uzs"] == "0"


def test_historical_snapshots_are_immutable_and_idempotency_intent_is_exact(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory(slug="immutable-a")
        branch_b = BranchFactory(slug="immutable-b")
        student = StudentProfileFactory(branch=branch_a)
        invoice = finance_services.issue_invoice(
            student_id=student.pk,
            lines=[
                {
                    "description": "Tuition",
                    "quantity": "1",
                    "unit_price_uzs": "100.00",
                }
            ],
        )
        payment, _ = payment_services.get_or_create_payment(
            idempotency_key="immutable-snapshot-payment",
            provider=Payment.Method.CLICK,
            amount_uzs=Decimal("100.00"),
            account_ref=invoice.number,
            invoice=invoice,
        )

        invoice.branch_at_issue = branch_b
        with pytest.raises(DjangoValidationError):
            invoice.save(update_fields=["branch_at_issue"])
        payment.branch_at_payment = branch_b
        with pytest.raises(DjangoValidationError):
            payment.save(update_fields=["branch_at_payment"])

        with pytest.raises(ConflictException) as reused:
            payment_services.get_or_create_payment(
                idempotency_key="immutable-snapshot-payment",
                provider=Payment.Method.PAYME,
                amount_uzs=Decimal("100.00"),
                account_ref=invoice.number,
                invoice=invoice,
            )
        assert reused.value.code == "idempotency_key_reused"

        invoice.refresh_from_db()
        payment.refresh_from_db()
        assert invoice.branch_at_issue_id == branch_a.pk
        assert payment.branch_at_payment_id == branch_a.pk


def test_statement_request_uses_invoice_snapshot_not_current_student_branch(
    tenant_a,
    user_in,
    as_user,
    monkeypatch,
):
    from celery_tasks.finance_tasks import generate_statement_pdf

    queued: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        generate_statement_pdf,
        "delay",
        lambda *args, **kwargs: queued.append((args, kwargs)) or SimpleNamespace(id="historical-statement"),
    )
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory(slug="statement-history-a")
        branch_b = BranchFactory(slug="statement-history-b")
        student = StudentProfileFactory(branch=branch_a)
        finance_services.issue_invoice(
            student_id=student.pk,
            lines=[{"description": "Tuition", "quantity": "1", "unit_price_uzs": "100.00"}],
        )
        StudentProfile.objects.filter(pk=student.pk).update(branch=branch_b)

    accountant_a = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch_a)
    accountant_b = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch_b)
    with schema_context(tenant_a.schema_name):
        from apps.org.models import StaffProfile

        StaffProfile.objects.create(
            user=accountant_a,
            username=f"statement-history-a-{accountant_a.pk}",
        )
        StaffProfile.objects.create(
            user=accountant_b,
            username=f"statement-history-b-{accountant_b.pk}",
        )
    response_a = as_user(tenant_a, accountant_a).post(
        f"/api/v1/finance/students/{student.pk}/statement/",
        {"locale": "en"},
        format="json",
    )
    response_b = as_user(tenant_a, accountant_b).post(
        f"/api/v1/finance/students/{student.pk}/statement/",
        {"locale": "en"},
        format="json",
    )
    assert response_a.status_code == 202
    assert response_b.status_code == 404
    assert len(queued) == 1
    export_id = response_a.json()["data"]["export_id"]
    assert queued[0][0] == (export_id,)
    assert queued[0][1] == {"_schema_name": tenant_a.schema_name}


def test_cross_branch_allocation_is_rejected_in_the_domain(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory(slug="allocation-a")
        branch_b = BranchFactory(slug="allocation-b")
        student_a = StudentProfileFactory(branch=branch_a)
        student_b = StudentProfileFactory(branch=branch_b)
        invoice_a = finance_services.issue_invoice(
            student_id=student_a.pk,
            lines=[{"description": "A", "quantity": "1", "unit_price_uzs": "100.00"}],
        )
        invoice_b = finance_services.issue_invoice(
            student_id=student_b.pk,
            lines=[{"description": "B", "quantity": "1", "unit_price_uzs": "100.00"}],
        )
        payment, _ = payment_services.get_or_create_payment(
            idempotency_key="cross-branch-allocation",
            provider=Payment.Method.CASH,
            amount_uzs=Decimal("100.00"),
            account_ref=invoice_a.number,
            invoice=invoice_a,
        )
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.COMPLETED)

        with pytest.raises(ValidationException) as exc:
            finance_services.allocate_payment_lines(
                payment_id=payment.pk,
                lines=[{"invoice": invoice_b.pk, "amount": "100.00"}],
            )
        assert exc.value.code == "allocation_scope_mismatch"
        assert not PaymentAllocation.objects.filter(payment_id=payment.pk).exists()


def test_refund_completion_rejects_cross_scope_payment_binding(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory(slug="refund-completion-a")
        branch_b = BranchFactory(slug="refund-completion-b")
        invoice_a = finance_services.issue_invoice(
            student_id=StudentProfileFactory(branch=branch_a).pk,
            lines=[{"description": "A", "quantity": "1", "unit_price_uzs": "100.00"}],
        )
        invoice_b = finance_services.issue_invoice(
            student_id=StudentProfileFactory(branch=branch_b).pk,
            lines=[{"description": "B", "quantity": "1", "unit_price_uzs": "100.00"}],
        )
        payment, _ = payment_services.get_or_create_payment(
            idempotency_key="cross-scope-refund-completion",
            provider=Payment.Method.PAYME,
            amount_uzs=Decimal("100.00"),
            account_ref=invoice_a.number,
            invoice=invoice_a,
        )
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.COMPLETED)
        refund = Refund.objects.create(
            invoice=invoice_b,
            amount_uzs=Decimal("50.00"),
            provider=Payment.Method.PAYME,
            state=Refund.State.SENT_TO_PROVIDER,
        )

        with pytest.raises(ValidationException) as exc:
            finance_services.register_refund_completion(
                refund_id=refund.pk,
                payment_id=payment.pk,
                provider=Payment.Method.PAYME,
                provider_refund_id="cross-scope-provider-refund",
            )
        assert exc.value.code == "refund_scope_mismatch"
        refund.refresh_from_db()
        assert refund.state == Refund.State.SENT_TO_PROVIDER
        assert refund.payment_id is None
        assert refund.ledger_entry_id is None


def test_backfill_is_dry_run_first_and_never_guesses_conflicts(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory(slug="backfill-a")
        branch_b = BranchFactory(slug="backfill-b")
        department_a = DepartmentFactory(branch=branch_a, slug="backfill-dept-a")
        cohort_a = CohortFactory(branch=branch_a, department=department_a)
        student_a = StudentProfileFactory(branch=branch_a)
        student_b = StudentProfileFactory(branch=branch_b)

        resolvable_invoice = _legacy_invoice(
            number="INV-BACKFILL-RESOLVED",
            student=student_a,
            cohort=cohort_a,
        )
        conflicting_invoice = _legacy_invoice(
            number="INV-BACKFILL-CONFLICT",
            student=student_a,
            cohort=cohort_a,
        )
        unresolved_invoice = _legacy_invoice(
            number="INV-BACKFILL-UNRESOLVED",
            student=student_b,
        )

        conflicting_evidence_payment = Payment.objects.create(
            provider=Payment.Method.CASH,
            amount_uzs=Decimal("100.00"),
            status=Payment.Status.COMPLETED,
            idempotency_key="backfill-conflicting-evidence",
            branch_at_payment=branch_b,
            attribution_status=ScopeAttributionStatus.CAPTURED,
        )
        PaymentAllocation.objects.create(
            invoice=conflicting_invoice,
            payment_id=conflicting_evidence_payment.pk,
            amount_uzs=Decimal("100.00"),
        )

        resolvable_payment = Payment.objects.create(
            provider=Payment.Method.CLICK,
            amount_uzs=Decimal("100.00"),
            status=Payment.Status.COMPLETED,
            idempotency_key="backfill-payment-resolved",
            account_ref=resolvable_invoice.number,
            metadata={"invoice_id": resolvable_invoice.pk},
        )
        shift_b = CashierShift.objects.create(
            cashier=UserFactory(),
            branch=branch_b,
        )
        conflicting_payment = Payment.objects.create(
            provider=Payment.Method.CASH,
            amount_uzs=Decimal("100.00"),
            status=Payment.Status.COMPLETED,
            idempotency_key="backfill-payment-conflict",
            account_ref=resolvable_invoice.number,
            metadata={"invoice_id": resolvable_invoice.pk},
            cashier_shift=shift_b,
        )
        unresolved_payment = Payment.objects.create(
            provider=Payment.Method.BANK_TRANSFER,
            amount_uzs=Decimal("10.00"),
            status=Payment.Status.COMPLETED,
            idempotency_key="backfill-payment-unresolved",
        )
        quarantined_payment = Payment.objects.create(
            provider=Payment.Method.BANK_TRANSFER,
            amount_uzs=Decimal("10.00"),
            status=Payment.Status.COMPLETED,
            idempotency_key="backfill-payment-quarantined",
            attribution_status=ScopeAttributionStatus.QUARANTINED,
        )

    dry_stdout = StringIO()
    call_command(
        "backfill_finance_attribution",
        "--schema",
        tenant_a.schema_name,
        stdout=dry_stdout,
    )
    dry_report = json.loads(dry_stdout.getvalue().splitlines()[-1])
    assert dry_report["mode"] == "dry_run"
    assert dry_report["totals"]["invoices"] == {
        "conflicting": 1,
        "quarantined": 0,
        "resolved": 1,
        "unresolved": 1,
    }
    assert dry_report["totals"]["payments"]["quarantined"] == 1
    assert dry_report["totals"]["payments"]["unresolved"] == 1
    assert resolvable_invoice.pk in {row["id"] for row in dry_report["schemas"][0]["invoices"]["review"]}
    assert conflicting_evidence_payment.pk in {
        row["id"] for row in dry_report["schemas"][0]["payments"]["review"]
    }

    with schema_context(tenant_a.schema_name):
        resolvable_invoice.refresh_from_db()
        conflicting_invoice.refresh_from_db()
        resolvable_payment.refresh_from_db()
        assert resolvable_invoice.attribution_status == ScopeAttributionStatus.UNRESOLVED
        assert conflicting_invoice.attribution_status == ScopeAttributionStatus.UNRESOLVED
        assert resolvable_payment.attribution_status == ScopeAttributionStatus.UNRESOLVED

    apply_stdout = StringIO()
    call_command(
        "backfill_finance_attribution",
        "--schema",
        tenant_a.schema_name,
        "--apply",
        "--quarantine-conflicts",
        stdout=apply_stdout,
    )
    apply_report = json.loads(apply_stdout.getvalue().splitlines()[-1])
    assert apply_report["mode"] == "apply"
    assert apply_report["totals"]["invoices"]["quarantined"] == 1
    assert apply_report["totals"]["payments"]["quarantined"] >= 2

    with schema_context(tenant_a.schema_name):
        for row in (
            resolvable_invoice,
            conflicting_invoice,
            unresolved_invoice,
            resolvable_payment,
            conflicting_payment,
            unresolved_payment,
            quarantined_payment,
            conflicting_evidence_payment,
        ):
            row.refresh_from_db()
        assert resolvable_invoice.attribution_status == ScopeAttributionStatus.RESOLVED
        assert resolvable_invoice.branch_at_issue_id == branch_a.pk
        assert resolvable_invoice.department_at_issue_id == department_a.pk
        assert conflicting_invoice.attribution_status == ScopeAttributionStatus.QUARANTINED
        assert conflicting_invoice.branch_at_issue_id is None
        assert unresolved_invoice.attribution_status == ScopeAttributionStatus.UNRESOLVED
        assert resolvable_payment.attribution_status == ScopeAttributionStatus.RESOLVED
        assert resolvable_payment.branch_at_payment_id == branch_a.pk
        assert conflicting_payment.attribution_status == ScopeAttributionStatus.QUARANTINED
        assert unresolved_payment.attribution_status == ScopeAttributionStatus.UNRESOLVED
        assert quarantined_payment.attribution_status == ScopeAttributionStatus.QUARANTINED
        # An already-captured row is never rewritten even when later evidence
        # conflicts; the report flags it for operator review.
        assert conflicting_evidence_payment.attribution_status == ScopeAttributionStatus.CAPTURED
        assert conflicting_evidence_payment.branch_at_payment_id == branch_b.pk


def test_backfill_fails_closed_if_resolved_invoice_evidence_has_no_branch(
    tenant_a,
    monkeypatch,
):
    from apps.finance.attribution import AttributionResolution
    from apps.finance.management.commands import backfill_finance_attribution as command_module

    with schema_context(tenant_a.schema_name):
        invoice = _legacy_invoice(
            number="INV-BACKFILL-INVALID-RESOLUTION",
            student=StudentProfileFactory(),
            cohort=CohortFactory(),
        )
        payment = Payment.objects.create(
            provider=Payment.Method.CASH,
            amount_uzs=Decimal("100.00"),
            status=Payment.Status.COMPLETED,
            idempotency_key="backfill-invalid-resolution",
            metadata={"invoice_id": invoice.pk},
        )
        monkeypatch.setattr(
            command_module,
            "resolve_scope_evidence",
            lambda _evidence: AttributionResolution(
                status=ScopeAttributionStatus.RESOLVED,
                branch_id=None,
                department_id=None,
                evidence=(),
            ),
        )

        with pytest.raises(CommandError, match=rf"invoice {invoice.pk} has no branch"):
            command_module.Command._invoice_evidence_for_payments([payment])


def test_backfill_refuses_the_public_platform_schema():
    with pytest.raises(CommandError):
        call_command(
            "backfill_finance_attribution",
            "--schema",
            get_public_schema_name(),
        )
