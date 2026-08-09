"""Finance lane tests (D3-A). Run centrally on Postgres.

Covers the DAY-3 Lane A "Tests required" list: happy-path invoice issue;
auto-issue-on-enrollment fires once; allocation exactness incl. a 3-way split of
an odd amount; cashier shift double-open rejected; parent sees only own
children's balances; cross-tenant isolation on /invoices/; query-count on the
invoice list (<=5). Plus sibling-discount materialization, the refund state
machine + register_refund_completion, payment plans, void, FX snapshot, and the
late-payment-reminder + statement task bodies.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import time_machine
from django.core.cache import cache
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.audit.models import AuditLog
from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
from apps.finance import selectors, services
from apps.finance.models import (
    CashierShift,
    Invoice,
    InvoiceLine,
    PaymentAllocation,
    Refund,
)
from apps.finance.signals import invoice_issued, payment_reminder
from apps.finance.tests.factories import (
    DiscountFactory,
    FeeScheduleFactory,
    InvoiceFactory,
)
from apps.org.models import CenterSettings
from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
from apps.students.models import StudentProfile
from apps.students.tests.factories import StudentProfileFactory
from core.exceptions import (
    ConflictException,
    PermissionException,
    UnprocessableEntity,
    ValidationException,
)
from core.permissions import Role

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "raw",
    [
        None,
        True,
        "",
        "NaN",
        "Infinity",
        "-1",
        "0",
        "0.00001",
        "100000000",
        "1000000001",
    ],
)
def test_fx_rate_validation_rejects_non_finite_or_out_of_range_values(raw):
    assert services.normalize_fx_rate(raw) is None


def test_fx_rate_validation_normalizes_valid_precision():
    assert services.normalize_fx_rate("12500.123456") == Decimal("12500.1235")


def test_usd_snapshot_omits_values_that_cannot_fit_the_money_column():
    assert services._usd_total(Decimal("9999999999999999.99"), Decimal("0.0001")) is None


def _settings(**kwargs):
    """Set CenterSettings fields (incl. the additive finance knobs once merged)
    and bust the cache. Skips fields not yet present on the model."""
    cs = CenterSettings.load()
    for key, value in kwargs.items():
        if hasattr(cs, key):
            setattr(cs, key, value)
    cs.save()
    cache.clear()
    return cs


def _captured_payment(invoice: Invoice, *, amount: str, key: str):
    from apps.payments.models import Payment

    return Payment.objects.create(
        provider=Payment.Method.CASH,
        amount_uzs=Decimal(amount),
        status=Payment.Status.COMPLETED,
        idempotency_key=key,
        account_ref=invoice.number,
        branch_at_payment_id=invoice.branch_at_issue_id,
        department_at_payment_id=invoice.department_at_issue_id,
        attribution_status="captured",
        metadata={"invoice_id": invoice.pk, "student_id": invoice.student_id},
    )


# --------------------------------------------------------------------------- #
# issue_invoice — happy path, numbering, FX snapshot
# --------------------------------------------------------------------------- #


def test_issue_invoice_happy_path_numbering_and_lines(tenant_a, django_capture_on_commit_callbacks):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        fs = FeeScheduleFactory(amount_uzs=Decimal("1500000.00"))
        captured = []
        invoice_issued.connect(lambda **kw: captured.append(kw), weak=False, dispatch_uid="t1")
        try:
            # execute=True runs the transaction.on_commit callback so the
            # emit-on-commit contract (services send signals on commit) is
            # exercised — mirrors the payments/attendance signal tests.
            with django_capture_on_commit_callbacks(execute=True):
                inv = services.issue_invoice(student_id=student.pk, fee_schedule_id=fs.pk)
        finally:
            invoice_issued.disconnect(dispatch_uid="t1")

        assert inv.status == Invoice.Status.ISSUED
        assert inv.number.startswith("INV-")
        assert inv.number.endswith("000001")
        assert inv.total_uzs == Decimal("1500000.00")
        assert inv.lines.count() == 1
        # signal fired once with the documented kwargs
        assert len(captured) == 1
        assert captured[0]["invoice_id"] == inv.pk
        assert captured[0]["student_id"] == student.pk


def test_issue_invoice_never_relabels_uzs_columns_as_presentation_currency(tenant_a):
    """A configurable display currency is not a ledger conversion rate."""
    with schema_context(tenant_a.schema_name):
        settings = CenterSettings.load()
        settings.currency_primary = "USD"
        settings.currency_secondary = "UZS"
        settings.save(update_fields=["currency_primary", "currency_secondary", "updated_at"])
        cache.clear()
        student = StudentProfileFactory()
        schedule = FeeScheduleFactory(amount_uzs=Decimal("15000.00"))

        invoice = services.issue_invoice(student_id=student.pk, fee_schedule_id=schedule.pk)

        assert invoice.total_uzs == Decimal("15000.00")
        assert invoice.currency == "UZS"


def test_issue_invoice_locks_unassigned_student_without_locking_nullable_cohort(tenant_a):
    """PostgreSQL must lock the student, not the nullable side of its cohort join."""
    with schema_context(tenant_a.schema_name):
        from django.db import connection

        student = StudentProfileFactory(current_cohort=None)
        fs = FeeScheduleFactory(amount_uzs=Decimal("1500000.00"))
        lock_queries: list[str] = []

        def capture_student_lock(execute, sql, params, many, context):
            if 'FROM "students_studentprofile"' in sql and "FOR UPDATE" in sql:
                lock_queries.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture_student_lock):
            invoice = services.issue_invoice(student_id=student.pk, fee_schedule_id=fs.pk)

        assert invoice.student_id == student.pk
        assert invoice.cohort_id is None
        assert invoice.branch_at_issue_id == student.branch_id
        assert len(lock_queries) == 1
        assert 'FOR UPDATE OF "students_studentprofile"' in lock_queries[0]


def test_issue_invoice_rejects_fee_schedule_from_another_cohort_in_same_branch(tenant_a):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student_cohort = CohortFactory(branch=branch)
        other_cohort = CohortFactory(branch=branch)
        student = StudentProfileFactory(branch=branch, current_cohort=student_cohort)
        fee_schedule = FeeScheduleFactory(cohort=other_cohort)

        with pytest.raises(ValidationException) as exc:
            services.issue_invoice(
                student_id=student.pk,
                fee_schedule_id=fee_schedule.pk,
            )
        assert exc.value.code == "fee_schedule_cohort_mismatch"


def test_issue_invoice_rechecks_locked_snapshot_against_authorized_scope(tenant_a):
    """The write service must fail closed if placement changes after HTTP scope lookup."""
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        authorized_branch = BranchFactory()
        current_branch = BranchFactory()
        cohort = CohortFactory(branch=current_branch)
        student = StudentProfileFactory(branch=current_branch, current_cohort=cohort)
        fee_schedule = FeeScheduleFactory(cohort=cohort)

        with pytest.raises(PermissionException) as exc:
            services.issue_invoice(
                student_id=student.pk,
                fee_schedule_id=fee_schedule.pk,
                allowed_scope_pairs={(authorized_branch.pk, None)},
            )

        assert exc.value.code == "out_of_scope"
        assert not Invoice.objects.filter(student=student).exists()


def test_issue_invoice_number_is_sequential_per_year(tenant_a):
    with schema_context(tenant_a.schema_name):
        fs = FeeScheduleFactory()
        s1, s2 = StudentProfileFactory(), StudentProfileFactory()
        i1 = services.issue_invoice(student_id=s1.pk, fee_schedule_id=fs.pk)
        i2 = services.issue_invoice(student_id=s2.pk, fee_schedule_id=fs.pk)
        n1 = int(i1.number.rsplit("-", 1)[1])
        n2 = int(i2.number.rsplit("-", 1)[1])
        assert n2 == n1 + 1


def test_invoice_numbering_takes_advisory_lock_even_for_first_invoice(tenant_a, monkeypatch):
    """MAJOR fix: a MAX()+1 select_for_update locks NO rows for the first invoice
    of a year, so two concurrent first issues both compute seq=1. The fix takes a
    per-(schema, year) transaction advisory lock that exists regardless of row
    count. Here we assert the lock is actually acquired on the empty-year path
    (a true concurrency race needs transaction=True; this proves the serialize
    point is reached) and that the first number is well-formed."""
    from apps.finance import services as finance_services

    calls: list[str] = []

    with schema_context(tenant_a.schema_name):
        from django.db import connection

        real_cursor = connection.cursor

        class _RecordingCursor:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=None):
                if "pg_advisory_xact_lock" in str(sql):
                    calls.append(str(sql))
                return self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        def fake_cursor():
            return _RecordingCursor(real_cursor())

        monkeypatch.setattr(connection, "cursor", fake_cursor)
        fs = FeeScheduleFactory()
        student = StudentProfileFactory()
        # No invoices yet this year -> the empty-year path must still lock.
        inv = finance_services.issue_invoice(student_id=student.pk, fee_schedule_id=fs.pk)
        assert inv.number.endswith("000001")
        assert calls, "issue_invoice must take a pg_advisory_xact_lock before numbering"


def test_issue_invoice_fx_snapshot_manual(tenant_a):
    with schema_context(tenant_a.schema_name):
        # The database deliberately rejects a transient manual-source row with
        # no rate.  Persist the mutually dependent settings atomically.
        cs = _settings(fx_source="manual", fx_rate_usd_manual=Decimal("12500.0000"))
        fs = FeeScheduleFactory(amount_uzs=Decimal("1250000.00"))
        student = StudentProfileFactory()
        inv = services.issue_invoice(student_id=student.pk, fee_schedule_id=fs.pk)
        if hasattr(cs, "fx_rate_usd_manual"):
            assert inv.fx_rate_usd == Decimal("12500.0000")
            assert inv.total_usd == Decimal("100.00")  # 1,250,000 / 12,500
        else:
            # knob not merged yet — snapshot is null, never a crash
            assert inv.total_usd is None or inv.total_usd >= Decimal("0")


def test_issue_invoice_explicit_lines(tenant_a):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        inv = services.issue_invoice(
            student_id=student.pk,
            lines=[
                {"description": "Books", "line_type": "material", "quantity": "2", "unit_price_uzs": "50000"},
            ],
        )
        assert inv.total_uzs == Decimal("100000.00")


def test_issue_invoice_empty_raises(tenant_a):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        with pytest.raises(ValidationException):
            services.issue_invoice(student_id=student.pk)


# --------------------------------------------------------------------------- #
# sibling discount materialization
# --------------------------------------------------------------------------- #


def test_sibling_discount_materializes_negative_line(tenant_a):
    with schema_context(tenant_a.schema_name):
        cs = CenterSettings.load()
        if not hasattr(cs, "sibling_discount_percent"):
            pytest.skip("sibling_discount_percent knob not merged yet (integration_needed)")
        cs.sibling_discount_percent = Decimal("10.00")
        cs.save()
        cache.clear()

        parent = ParentProfileFactory()
        s1 = StudentProfileFactory(status=StudentProfile.Status.ENROLLED)
        s2 = StudentProfileFactory(status=StudentProfile.Status.ENROLLED)
        GuardianFactory(parent=parent, student=s1)
        GuardianFactory(parent=parent, student=s2)

        fs = FeeScheduleFactory(amount_uzs=Decimal("1000000.00"))
        inv = services.issue_invoice(student_id=s1.pk, fee_schedule_id=fs.pk)
        discount_lines = inv.lines.filter(line_type=InvoiceLine.LineType.DISCOUNT)
        assert discount_lines.count() == 1
        assert discount_lines.first().amount_uzs == Decimal("-100000.00")
        assert inv.total_uzs == Decimal("900000.00")


def test_no_sibling_discount_without_enrolled_sibling(tenant_a):
    with schema_context(tenant_a.schema_name):
        cs = CenterSettings.load()
        if not hasattr(cs, "sibling_discount_percent"):
            pytest.skip("sibling_discount_percent knob not merged yet")
        cs.sibling_discount_percent = Decimal("10.00")
        cs.save()
        cache.clear()
        solo = StudentProfileFactory(status=StudentProfile.Status.ENROLLED)
        fs = FeeScheduleFactory(amount_uzs=Decimal("1000000.00"))
        inv = services.issue_invoice(student_id=solo.pk, fee_schedule_id=fs.pk)
        assert inv.lines.filter(line_type=InvoiceLine.LineType.DISCOUNT).count() == 0
        assert inv.total_uzs == Decimal("1000000.00")


def test_standing_discount_materializes(tenant_a):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        DiscountFactory(student=student, percent=Decimal("20.00"), fixed_amount_uzs=None)
        fs = FeeScheduleFactory(amount_uzs=Decimal("500000.00"))
        inv = services.issue_invoice(student_id=student.pk, fee_schedule_id=fs.pk)
        assert inv.total_uzs == Decimal("400000.00")


def test_stacked_discounts_are_capped_at_the_charge(tenant_a):
    """R1-04: discounts summing beyond 100% must floor the bill at 0 without driving
    the persisted InvoiceLine rows negative — total_uzs and sum(lines) must agree."""
    from apps.finance.models import InvoiceLine

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        # Two 60%-of-gross discounts = 120% > 100%; the aggregate must cap at the charge.
        DiscountFactory(student=student, percent=Decimal("60.00"), fixed_amount_uzs=None)
        DiscountFactory(student=student, percent=Decimal("60.00"), fixed_amount_uzs=None)
        fs = FeeScheduleFactory(amount_uzs=Decimal("500000.00"))
        inv = services.issue_invoice(student_id=student.pk, fee_schedule_id=fs.pk)
        assert inv.total_uzs == Decimal("0.00")
        line_sum = sum((line.amount_uzs for line in InvoiceLine.objects.filter(invoice=inv)), Decimal("0"))
        assert line_sum == inv.total_uzs  # invariant holds: no negative persisted balance


def test_partial_payment_on_past_due_invoice_is_immediately_overdue(tenant_a):
    """A payment refresh must not hide a delinquent balance until the next beat."""
    from datetime import timedelta

    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        inv = services.issue_invoice(
            student_id=student.pk,
            lines=[
                {
                    "description": "Tuition",
                    "line_type": "tuition",
                    "quantity": "1",
                    "unit_price_uzs": "1000000",
                }
            ],
        )
        past = timezone.now().date() - timedelta(days=10)
        Invoice.objects.filter(pk=inv.pk).update(due_date=past)
        pay = Payment.objects.create(
            provider="cash",
            amount_uzs=Decimal("300000.00"),
            status="completed",
            idempotency_key="r2p2-partial-overdue",
            branch_at_payment_id=inv.branch_at_issue_id,
            department_at_payment_id=inv.department_at_issue_id,
            attribution_status="captured",
        )
        services.allocate_payment(payment_id=pay.pk, invoice_ids=[inv.pk], amount_uzs=Decimal("300000.00"))
        inv.refresh_from_db()
        assert inv.status == Invoice.Status.OVERDUE
        # The reminder job remains idempotently compatible with the status that
        # the write transaction has already derived.
        services.emit_payment_reminders()
        inv.refresh_from_db()
        assert inv.status == Invoice.Status.OVERDUE


@time_machine.travel("2026-06-05 12:30:00 +00:00", tick=False)
def test_invoice_refresh_uses_organization_local_calendar_day(tenant_a):
    """UTC is still June 5 while Pacific/Kiritimati is already June 6."""
    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name), timezone.override("UTC"):
        settings = CenterSettings.load()
        settings.organization_timezone = "Pacific/Kiritimati"
        settings.save(update_fields=["organization_timezone", "updated_at"])
        cache.clear()
        invoice = InvoiceFactory(due_date=date(2026, 6, 5), total_uzs=Decimal("100.00"))
        payment = Payment.objects.create(
            provider=Payment.Method.CASH,
            amount_uzs=Decimal("10.00"),
            status=Payment.Status.COMPLETED,
            idempotency_key="organization-local-overdue",
            branch_at_payment_id=invoice.branch_at_issue_id,
            department_at_payment_id=invoice.department_at_issue_id,
            attribution_status="captured",
        )

        services.allocate_payment(
            payment_id=payment.pk,
            invoice_ids=[invoice.pk],
            amount_uzs=Decimal("10.00"),
        )

        invoice.refresh_from_db()
        assert timezone.localdate() == date(2026, 6, 5)  # caller context remains UTC
        assert invoice.status == Invoice.Status.OVERDUE  # organization day is June 6


# --------------------------------------------------------------------------- #
# auto-issue on enrollment (idempotent / fires once)
# --------------------------------------------------------------------------- #


def test_auto_issue_on_enrollment_creates_one_invoice(tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory()
        student = StudentProfileFactory(current_cohort=cohort, branch=cohort.branch)
        FeeScheduleFactory(cohort=cohort, amount_uzs=Decimal("800000.00"))

        inv = services.auto_issue_on_enrollment(student_id=student.pk, cohort_id=cohort.pk)
        assert inv is not None
        assert Invoice.objects.filter(student=student).count() == 1

        # Re-firing the signal body must NOT duplicate (dedupe on student/fs/period)
        again = services.auto_issue_on_enrollment(student_id=student.pk, cohort_id=cohort.pk)
        assert again.pk == inv.pk
        assert Invoice.objects.filter(student=student).count() == 1


def test_auto_issue_locks_student_before_idempotency_check(tenant_a):
    """A repeated callback must serialize before its dedupe read.

    The existing-invoice path never reaches ``issue_invoice``, so any student
    ``FOR UPDATE`` observed here belongs to ``auto_issue_on_enrollment`` itself.
    This prevents two callbacks from both observing an empty dedupe key.
    """
    with schema_context(tenant_a.schema_name):
        from django.db import connection

        cohort = CohortFactory()
        student = StudentProfileFactory(current_cohort=cohort, branch=cohort.branch)
        FeeScheduleFactory(cohort=cohort, amount_uzs=Decimal("800000.00"))
        first = services.auto_issue_on_enrollment(
            student_id=student.pk,
            cohort_id=cohort.pk,
        )
        assert first is not None

        lock_queries: list[str] = []

        def capture_student_lock(execute, sql, params, many, context):
            if 'FROM "students_studentprofile"' in sql and "FOR UPDATE" in sql:
                lock_queries.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture_student_lock):
            repeated = services.auto_issue_on_enrollment(
                student_id=student.pk,
                cohort_id=cohort.pk,
            )

        assert repeated is not None
        assert repeated.pk == first.pk
        assert len(lock_queries) == 1
        assert 'FOR UPDATE OF "students_studentprofile"' in lock_queries[0]


def test_auto_issue_falls_back_to_center_wide_schedule(tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory()
        student = StudentProfileFactory(current_cohort=cohort, branch=cohort.branch)
        FeeScheduleFactory(cohort=None, amount_uzs=Decimal("700000.00"))  # center-wide
        inv = services.auto_issue_on_enrollment(student_id=student.pk, cohort_id=cohort.pk)
        assert inv is not None
        assert inv.total_uzs == Decimal("700000.00")


def test_auto_issue_no_matching_schedule_returns_none(tenant_a):
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory()
        student = StudentProfileFactory(current_cohort=cohort, branch=cohort.branch)
        assert services.auto_issue_on_enrollment(student_id=student.pk, cohort_id=cohort.pk) is None
        assert Invoice.objects.filter(student=student).count() == 0


def test_enrollment_signal_receiver_issues_once(tenant_a, django_capture_on_commit_callbacks):
    """The receiver is wired to cohort_member_moved; firing it issues exactly one
    invoice and a second fire dedupes."""
    from apps.cohorts.services import move_student
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        # move_student now enforces student.branch == to_cohort.branch, so pin all
        # three to one branch (the cross-branch case is a separate negative test).
        branch = BranchFactory()
        source = CohortFactory(branch=branch)
        target = CohortFactory(branch=branch)
        student = StudentProfileFactory(current_cohort=source, branch=branch)
        CohortMembershipFactory(cohort=source, student=student)
        FeeScheduleFactory(cohort=target, amount_uzs=Decimal("600000.00"))

        # move_student emits cohort_member_moved inside transaction.on_commit;
        # execute=True runs that callback so the finance receiver auto-issues.
        with django_capture_on_commit_callbacks(execute=True):
            move_student(student=student, to_cohort=target)
        assert Invoice.objects.filter(student=student).count() == 1


# --------------------------------------------------------------------------- #
# allocate_payment — exactness, 3-way odd split, status flips, over-allocation
# --------------------------------------------------------------------------- #


def test_allocate_payment_single_invoice_marks_paid(tenant_a):
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("1000000.00"))
        payment = _captured_payment(inv, amount="1000000.00", key="allocate-single")
        allocs = services.allocate_payment(
            payment_id=payment.pk,
            amount_uzs=Decimal("1000000.00"),
        )
        assert len(allocs) == 1
        inv.refresh_from_db()
        assert inv.status == Invoice.Status.PAID


def test_allocate_payment_rejects_unconfirmed_payment(tenant_a):
    with schema_context(tenant_a.schema_name):
        from apps.payments.models import Payment

        invoice = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(invoice, amount="100000.00", key="allocate-pending")
        payment.status = Payment.Status.PENDING
        payment.save(update_fields=["status", "updated_at"])

        with pytest.raises(UnprocessableEntity) as exc:
            services.allocate_payment(
                payment_id=payment.pk,
                amount_uzs=Decimal("100000.00"),
            )

        assert exc.value.code == "payment_not_completed"
        assert not PaymentAllocation.objects.filter(payment_id=payment.pk).exists()
        invoice.refresh_from_db()
        assert invoice.status == Invoice.Status.ISSUED


def test_allocate_payment_partial_marks_partially_paid(tenant_a):
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("1000000.00"), due_date=date(2099, 6, 5))
        payment = _captured_payment(inv, amount="400000.00", key="allocate-partial")
        services.allocate_payment(payment_id=payment.pk, amount_uzs=Decimal("400000.00"))
        inv.refresh_from_db()
        assert inv.status == Invoice.Status.PARTIALLY_PAID


def test_allocate_payment_oldest_due_first(tenant_a):
    from datetime import date

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        old = InvoiceFactory(student=student, due_date=date(2026, 1, 1), total_uzs=Decimal("300000.00"))
        new = InvoiceFactory(student=student, due_date=date(2026, 6, 1), total_uzs=Decimal("300000.00"))
        payment = _captured_payment(old, amount="300000.00", key="allocate-oldest")
        services.allocate_payment(payment_id=payment.pk, amount_uzs=Decimal("300000.00"))
        old.refresh_from_db()
        new.refresh_from_db()
        assert old.status == Invoice.Status.PAID
        assert new.status == Invoice.Status.ISSUED


def test_allocate_payment_three_way_odd_split_is_exact(tenant_a):
    """Awkward amount over 3 invoices: sum(allocations) == amount EXACTLY (no
    rounding loss), Decimal throughout."""
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        invoices = [InvoiceFactory(student=student, total_uzs=Decimal("333333.34")) for _ in range(3)]
        amount = Decimal("1000000.01")
        payment = _captured_payment(invoices[0], amount=str(amount), key="allocate-three-way")
        allocs = services.allocate_payment(payment_id=payment.pk, amount_uzs=amount)
        total = sum((a.amount_uzs for a in allocs), Decimal("0"))
        assert total == amount
        for a in allocs:
            assert isinstance(a.amount_uzs, Decimal)


def test_allocate_payment_over_allocation_raises(tenant_a):
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(invoice, amount="200000.00", key="allocate-over")
        with pytest.raises(ValidationException) as exc:
            services.allocate_payment(
                payment_id=payment.pk,
                amount_uzs=Decimal("200000.00"),
            )
        assert exc.value.code == "over_allocation"


def test_allocate_payment_idempotent_on_payment_id(tenant_a):
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(invoice, amount="100000.00", key="allocate-idempotent")
        first = services.allocate_payment(
            payment_id=payment.pk,
            amount_uzs=Decimal("100000.00"),
        )
        again = services.allocate_payment(
            payment_id=payment.pk,
            amount_uzs=Decimal("100000.00"),
        )
        assert {a.pk for a in first} == {a.pk for a in again}
        assert PaymentAllocation.objects.filter(payment_id=payment.pk).count() == 1


def test_allocate_payment_rejects_more_than_received_and_mismatched_retry(tenant_a):
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(total_uzs=Decimal("200000.00"))
        payment = _captured_payment(invoice, amount="100000.00", key="allocate-intent-bound")
        with pytest.raises(ValidationException) as exceeds:
            services.allocate_payment(
                payment_id=payment.pk,
                amount_uzs=Decimal("150000.00"),
            )
        assert exceeds.value.code == "allocation_exceeds_payment"

        with pytest.raises(ValidationException) as partial:
            services.allocate_payment(
                payment_id=payment.pk,
                amount_uzs=Decimal("90000.00"),
            )
        assert partial.value.code == "allocation_total_mismatch"

        services.allocate_payment(
            payment_id=payment.pk,
            amount_uzs=Decimal("100000.00"),
            invoice_ids=[invoice.pk],
        )
        with pytest.raises(ConflictException) as mismatch:
            services.allocate_payment(
                payment_id=payment.pk,
                amount_uzs=Decimal("90000.00"),
                invoice_ids=[invoice.pk],
            )
        assert mismatch.value.code == "allocation_intent_mismatch"


def test_allocate_payment_rejects_matching_retry_of_partial_existing_rows(tenant_a):
    """Legacy partial rows must not turn a stranded payment into a valid retry."""
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(invoice, amount="50000.00", key="allocate-partial-existing")
        PaymentAllocation.objects.create(
            payment_id=payment.pk,
            invoice=invoice,
            amount_uzs=Decimal("40000.00"),
        )

        # Keep changed retries on the documented idempotency-conflict contract
        # before inspecting the health of the committed row set.
        with pytest.raises(ConflictException) as changed:
            services.allocate_payment(
                payment_id=payment.pk,
                amount_uzs=Decimal("50000.00"),
                invoice_ids=[invoice.pk],
            )
        assert changed.value.code == "allocation_intent_mismatch"

        with pytest.raises(ConflictException) as corrupt:
            services.allocate_payment(
                payment_id=payment.pk,
                amount_uzs=Decimal("40000.00"),
                invoice_ids=[invoice.pk],
            )
        assert corrupt.value.code == "allocation_state_inconsistent"
        payment.refresh_from_db()
        assert "_allocation_intent" not in payment.metadata
        assert PaymentAllocation.objects.filter(payment_id=payment.pk).count() == 1


# --------------------------------------------------------------------------- #
# cashier shift open/close + double-open guard + report
# --------------------------------------------------------------------------- #


def test_cashier_shift_double_open_rejected(tenant_a, user_in):
    from apps.org.tests.factories import BranchFactory

    cashier = user_in(tenant_a, roles=[Role.CASHIER])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        services.open_cashier_shift(cashier=cashier, branch=branch)
        with pytest.raises(ConflictException) as exc:
            services.open_cashier_shift(cashier=cashier, branch=branch)
        assert exc.value.code == "shift_already_open"


def test_cashier_shift_close_computes_discrepancy(tenant_a, user_in):
    from apps.org.tests.factories import BranchFactory

    cashier = user_in(tenant_a, roles=[Role.CASHIER])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        shift = services.open_cashier_shift(
            cashier=cashier, branch=branch, opening_cash_uzs=Decimal("50000.00")
        )
        closed = services.close_cashier_shift(
            shift=shift, closing_cash_uzs=Decimal("70000.00"), actor=cashier
        )
        # no payments merged -> discrepancy = 70000 - (50000 + 0) = 20000
        assert closed.discrepancy_uzs == Decimal("20000.00")
        assert closed.status == CashierShift.Status.CLOSED


def test_cashier_report_tolerates_zero_payments(tenant_a, user_in):
    from apps.org.tests.factories import BranchFactory

    cashier = user_in(tenant_a, roles=[Role.CASHIER])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        shift = services.open_cashier_shift(cashier=cashier, branch=branch)
        report = selectors.cashier_shift_report(shift=shift)
        assert report["payments_total_uzs"] == "0.00"
        assert report["totals_by_provider"] == {}


# --------------------------------------------------------------------------- #
# payment plan
# --------------------------------------------------------------------------- #


def test_payment_plan_must_sum_to_total(tenant_a):
    from datetime import date

    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("1000000.00"))
        with pytest.raises(ValidationException) as exc:
            services.create_payment_plan(
                invoice=inv,
                installments=[{"due_date": date(2026, 7, 5), "amount_uzs": "999999.00"}],
            )
        assert exc.value.code == "plan_sum_mismatch"


def test_payment_plan_rejects_negative_offset_installment(tenant_a):
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        with pytest.raises(ValidationException) as exc:
            services.create_payment_plan(
                invoice=inv,
                installments=[
                    {"due_date": date(2026, 7, 5), "amount_uzs": "110000.00"},
                    {"due_date": date(2026, 8, 5), "amount_uzs": "-10000.00"},
                ],
            )
        assert exc.value.code == "invalid_installment_amount"


def test_payment_plan_happy(tenant_a):
    from datetime import date

    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("1000000.00"))
        plan = services.create_payment_plan(
            invoice=inv,
            installments=[
                {"due_date": date(2026, 7, 5), "amount_uzs": "500000.00"},
                {"due_date": date(2026, 8, 5), "amount_uzs": "500000.00"},
            ],
        )
        assert plan.installments.count() == 2


# --------------------------------------------------------------------------- #
# refund state machine + register_refund_completion (Lane B entry point)
# --------------------------------------------------------------------------- #


def test_refund_illegal_transition_raises(tenant_a):
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(inv, amount="100000.00", key="refund-illegal")
        services.allocate_payment(payment_id=payment.pk, amount_uzs=Decimal("100000.00"))
        refund = services.request_refund(
            invoice=inv,
            amount_uzs=Decimal("100000.00"),
            payment_id=payment.pk,
        )
        with pytest.raises(ValidationException) as exc:
            services.transition_refund(refund_id=refund.pk, to_state=Refund.State.COMPLETED)
        assert exc.value.code == "invalid_refund_transition"


def test_register_refund_completion_idempotent(tenant_a):
    from apps.approvals.models import LedgerEntry

    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(inv, amount="100000.00", key="refund-idempotent")
        services.allocate_payment(payment_id=payment.pk, amount_uzs=Decimal("100000.00"))
        refund = services.request_refund(invoice=inv, amount_uzs=Decimal("100000.00"))
        done = services.register_refund_completion(
            refund.pk,
            payment_id=payment.pk,
            provider="payme",
            provider_refund_id=f"payme-{payment.pk}",
        )
        assert done.state == Refund.State.COMPLETED
        assert done.payment_id == payment.pk
        assert done.provider == "payme"
        assert done.provider_confirmed_at is not None
        assert done.ledger_entry_id is not None
        ledger_entry_id = done.ledger_entry_id
        again = services.register_refund_completion(
            refund.pk,
            payment_id=payment.pk,
            provider="payme",
            provider_refund_id=f"payme-{payment.pk}",
        )
        assert again.state == Refund.State.COMPLETED
        assert again.ledger_entry_id == ledger_entry_id
        assert LedgerEntry.objects.filter(source_kind="refund", source_id=refund.pk).count() == 1


def test_register_refund_completion_rejects_reused_provider_reference(tenant_a):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        first_invoice = InvoiceFactory(student=student, total_uzs=Decimal("100.00"))
        second_invoice = InvoiceFactory(student=student, total_uzs=Decimal("100.00"))
        first_payment = _captured_payment(first_invoice, amount="100.00", key="refund-ref-first")
        second_payment = _captured_payment(second_invoice, amount="100.00", key="refund-ref-second")
        services.allocate_payment(payment_id=first_payment.pk, amount_uzs=Decimal("100.00"))
        services.allocate_payment(payment_id=second_payment.pk, amount_uzs=Decimal("100.00"))
        first_refund = services.request_refund(
            invoice=first_invoice,
            amount_uzs=Decimal("100.00"),
            payment_id=first_payment.pk,
        )
        second_refund = services.request_refund(
            invoice=second_invoice,
            amount_uzs=Decimal("100.00"),
            payment_id=second_payment.pk,
        )
        services.register_refund_completion(
            first_refund.pk,
            provider="payme",
            provider_refund_id="provider-ref-once",
        )

        with pytest.raises(ConflictException) as exc:
            services.register_refund_completion(
                second_refund.pk,
                provider="payme",
                provider_refund_id="provider-ref-once",
            )

        assert exc.value.code == "refund_confirmation_reused"
        second_refund.refresh_from_db()
        assert second_refund.state == Refund.State.REQUESTED
        assert second_refund.ledger_entry_id is None


def test_register_refund_completion_requires_matching_provider_confirmation(tenant_a):
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(inv, amount="100000.00", key="refund-provider-match")
        services.allocate_payment(payment_id=payment.pk, amount_uzs=Decimal("100000.00"))
        refund = services.request_refund(
            invoice=inv,
            amount_uzs=Decimal("100000.00"),
            payment_id=payment.pk,
            provider="click",
        )
        with pytest.raises(ValidationException) as missing:
            services.register_refund_completion(
                refund.pk,
                payment_id=payment.pk,
                provider="click",
                provider_refund_id="",
            )
        assert missing.value.code == "provider_confirmation_required"
        with pytest.raises(ValidationException) as mismatch:
            services.register_refund_completion(
                refund.pk,
                payment_id=payment.pk,
                provider="payme",
                provider_refund_id="payme-210",
            )
        assert mismatch.value.code == "refund_provider_mismatch"
        refund.refresh_from_db()
        assert refund.state == Refund.State.REQUESTED
        assert refund.ledger_entry_id is None


def test_register_refund_completion_requires_a_source_payment(tenant_a):
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(invoice, amount="100000.00", key="refund-source-required")
        services.allocate_payment(
            payment_id=payment.pk,
            amount_uzs=Decimal("100000.00"),
        )
        refund = services.request_refund(
            invoice=invoice,
            amount_uzs=Decimal("100000.00"),
        )

        with pytest.raises(ValidationException) as exc:
            services.register_refund_completion(
                refund.pk,
                provider="payme",
                provider_refund_id="missing-source-payment",
            )
        assert exc.value.code == "refund_payment_required"
        refund.refresh_from_db()
        assert refund.state == Refund.State.REQUESTED
        assert refund.ledger_entry_id is None


def test_refund_exceeds_paid_rejected(tenant_a):
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(inv, amount="50000.00", key="refund-exceeds-paid")
        services.allocate_payment(payment_id=payment.pk, amount_uzs=Decimal("50000.00"))
        with pytest.raises(ValidationException) as exc:
            services.request_refund(invoice=inv, amount_uzs=Decimal("90000.00"))
        assert exc.value.code == "refund_exceeds_paid"


def test_refund_exceeds_single_payment_contribution_rejected(tenant_a):
    """On a multi-payment invoice, one payment can't be refunded for more than IT
    contributed, even though the invoice-level paid total would allow it."""
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment_a = _captured_payment(inv, amount="50000.00", key="refund-part-a")
        payment_b = _captured_payment(inv, amount="50000.00", key="refund-part-b")
        services.allocate_payment(payment_id=payment_a.pk, amount_uzs=Decimal("50000.00"))
        services.allocate_payment(payment_id=payment_b.pk, amount_uzs=Decimal("50000.00"))
        # Invoice net_paid is 100000, but payment 70 only contributed 50000.
        with pytest.raises(ValidationException) as exc:
            services.request_refund(
                invoice=inv,
                amount_uzs=Decimal("80000.00"),
                payment_id=payment_a.pk,
            )
        assert exc.value.code == "refund_exceeds_payment"
        # A refund up to the payment's own contribution still succeeds.
        ok = services.request_refund(
            invoice=inv,
            amount_uzs=Decimal("50000.00"),
            payment_id=payment_a.pk,
        )
        assert ok.amount_uzs == Decimal("50000.00")


def test_refund_reversal_is_scoped_to_the_named_invoice(tenant_a):
    """R5/CONF1 (money): one payment split across invoices A+B; a refund attributed to
    A must release ONLY A's allocation, leaving B's allocation (and PAID status) intact.
    The reversal was payment-scoped (newest-first across all invoices), so a refund of A
    silently reopened B."""
    from datetime import date

    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        inv_a = InvoiceFactory(student=student, total_uzs=Decimal("100000.00"), due_date=date(2026, 1, 1))
        inv_b = InvoiceFactory(student=student, total_uzs=Decimal("60000.00"), due_date=date(2026, 2, 1))
        pay = Payment.objects.create(
            provider="cash",
            amount_uzs=Decimal("160000.00"),
            status="completed",
            idempotency_key="r5c1",
            branch_at_payment_id=inv_a.branch_at_issue_id,
            department_at_payment_id=inv_a.department_at_issue_id,
            attribution_status="captured",
        )
        services.allocate_payment(
            payment_id=pay.pk, invoice_ids=[inv_a.pk, inv_b.pk], amount_uzs=Decimal("160000.00")
        )
        inv_a.refresh_from_db()
        inv_b.refresh_from_db()
        assert inv_a.status == Invoice.Status.PAID
        assert inv_b.status == Invoice.Status.PAID

        # Refund A's full 100000 via the payment; only A must reopen.
        refund = services.request_refund(invoice=inv_a, amount_uzs=Decimal("100000.00"), payment_id=pay.pk)
        services.register_refund_completion(
            refund.pk,
            payment_id=pay.pk,
            provider="payme",
            provider_refund_id=f"payme-{pay.pk}-a",
        )

        inv_a.refresh_from_db()
        inv_b.refresh_from_db()
        assert inv_a.status != Invoice.Status.PAID  # A reopened (its allocation released)
        assert inv_b.status == Invoice.Status.PAID  # B UNTOUCHED (not silently reopened)
        assert PaymentAllocation.objects.filter(payment_id=pay.pk, invoice=inv_b).exists()
        assert not PaymentAllocation.objects.filter(payment_id=pay.pk, invoice=inv_a).exists()


def test_refund_ceiling_is_payment_intersect_invoice_not_payment_wide(tenant_a):
    """R6-01 (money): the per-payment refund ceiling must equal what the invoice-scoped
    reversal can actually release — the payment's allocation TO THIS invoice. A
    payment-wide ceiling passed a refund larger than the payment's share of the invoice,
    but the reversal released only that share => money out with no restored receivable."""
    from datetime import date

    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        inv_a = InvoiceFactory(student=student, total_uzs=Decimal("150000.00"), due_date=date(2026, 1, 1))
        inv_b = InvoiceFactory(student=student, total_uzs=Decimal("60000.00"), due_date=date(2026, 2, 1))
        p = Payment.objects.create(
            provider="cash",
            amount_uzs=Decimal("160000.00"),
            status="completed",
            idempotency_key="r6p",
            branch_at_payment_id=inv_a.branch_at_issue_id,
            department_at_payment_id=inv_a.department_at_issue_id,
            attribution_status="captured",
        )
        q = Payment.objects.create(
            provider="cash",
            amount_uzs=Decimal("50000.00"),
            status="completed",
            idempotency_key="r6q",
            branch_at_payment_id=inv_a.branch_at_issue_id,
            department_at_payment_id=inv_a.department_at_issue_id,
            attribution_status="captured",
        )
        # Manual split: P 100000 -> A, 60000 -> B; Q 50000 -> A. Invoice A is PAID (150000).
        services.allocate_payment_lines(
            payment_id=p.pk,
            lines=[{"invoice": inv_a.pk, "amount": "100000.00"}, {"invoice": inv_b.pk, "amount": "60000.00"}],
        )
        services.allocate_payment_lines(payment_id=q.pk, lines=[{"invoice": inv_a.pk, "amount": "50000.00"}])
        # A refund citing P on A for 150000 must be REJECTED — P only contributed 100000
        # to A, and the invoice-scoped reversal could release only that 100000.
        with pytest.raises(ValidationException) as exc:
            services.request_refund(invoice=inv_a, amount_uzs=Decimal("150000.00"), payment_id=p.pk)
        assert exc.value.code == "refund_exceeds_payment"
        # A refund up to P's contribution TO A (100000) is allowed and fully reverses.
        refund = services.request_refund(invoice=inv_a, amount_uzs=Decimal("100000.00"), payment_id=p.pk)
        services.register_refund_completion(
            refund.pk,
            payment_id=p.pk,
            provider="payme",
            provider_refund_id=f"payme-{p.pk}-a",
        )
        assert not PaymentAllocation.objects.filter(payment_id=p.pk, invoice=inv_a).exists()


def test_register_refund_completion_reverses_allocation_and_status(tenant_a):
    """BLOCKER fix: completing a refund must delete the PaymentAllocation rows and
    flip the invoice off PAID, restoring the outstanding balance."""
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        inv = InvoiceFactory(
            student=student,
            total_uzs=Decimal("100000.00"),
            due_date=date(2099, 6, 5),
        )
        payment = _captured_payment(inv, amount="100000.00", key="refund-reversal")
        services.allocate_payment(
            payment_id=payment.pk,
            invoice_ids=[inv.pk],
            amount_uzs=Decimal("100000.00"),
        )
        inv.refresh_from_db()
        assert inv.status == Invoice.Status.PAID
        assert selectors.outstanding_balance(student.pk) == Decimal("0.00")

        refund = services.request_refund(
            invoice=inv,
            amount_uzs=Decimal("100000.00"),
            payment_id=payment.pk,
        )
        services.register_refund_completion(
            refund.pk,
            payment_id=payment.pk,
            provider="payme",
            provider_refund_id=f"payme-{payment.pk}",
        )

        inv.refresh_from_db()
        assert inv.status == Invoice.Status.ISSUED
        assert PaymentAllocation.objects.filter(payment_id=payment.pk).count() == 0
        assert selectors.outstanding_balance(student.pk) == Decimal("100000.00")


def test_partial_refund_reverses_only_refunded_amount(tenant_a):
    """A partial refund releases only its amount; the invoice drops to
    PARTIALLY_PAID and the balance reflects the still-paid remainder."""
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        inv = InvoiceFactory(
            student=student,
            total_uzs=Decimal("100000.00"),
            due_date=date(2099, 6, 5),
        )
        payment = _captured_payment(inv, amount="100000.00", key="refund-partial")
        services.allocate_payment(
            payment_id=payment.pk,
            invoice_ids=[inv.pk],
            amount_uzs=Decimal("100000.00"),
        )

        refund = services.request_refund(
            invoice=inv,
            amount_uzs=Decimal("30000.00"),
            payment_id=payment.pk,
        )
        services.register_refund_completion(
            refund.pk,
            payment_id=payment.pk,
            provider="payme",
            provider_refund_id=f"payme-{payment.pk}",
        )

        inv.refresh_from_db()
        assert inv.status == Invoice.Status.PARTIALLY_PAID
        # 30000 released -> 70000 still allocated -> outstanding 30000
        assert selectors.outstanding_balance(student.pk) == Decimal("30000.00")
        from django.db.models import Sum

        remaining = PaymentAllocation.objects.filter(payment_id=payment.pk).aggregate(s=Sum("amount_uzs"))[
            "s"
        ]
        assert remaining == Decimal("70000.00")


@time_machine.travel("2026-06-10 12:00:00 +05:00", tick=False)
def test_refund_reopens_past_due_invoice_as_overdue_in_same_transaction(tenant_a):
    """A refund race must not briefly turn delinquent receivables into current ones."""
    with schema_context(tenant_a.schema_name):
        invoice = InvoiceFactory(
            total_uzs=Decimal("100000.00"),
            due_date=date(2026, 6, 5),
        )
        payment = _captured_payment(invoice, amount="100000.00", key="refund-overdue-refresh")
        services.allocate_payment(
            payment_id=payment.pk,
            invoice_ids=[invoice.pk],
            amount_uzs=Decimal("100000.00"),
        )
        refund = services.request_refund(
            invoice=invoice,
            amount_uzs=Decimal("30000.00"),
            payment_id=payment.pk,
        )

        services.register_refund_completion(
            refund.pk,
            payment_id=payment.pk,
            provider="payme",
            provider_refund_id="past-due-refund-confirmed",
        )

        invoice.refresh_from_db()
        assert invoice.status == Invoice.Status.OVERDUE
        assert selectors.outstanding_balance(invoice.student_id) == Decimal("30000.00")


def test_second_refund_after_completion_rejected(tenant_a):
    """MAJOR fix: once a refund completes (and its allocation is reversed), a
    second full refund on the same invoice is rejected — the net paid is gone."""
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(inv, amount="100000.00", key="refund-second")
        services.allocate_payment(
            payment_id=payment.pk,
            invoice_ids=[inv.pk],
            amount_uzs=Decimal("100000.00"),
        )

        refund = services.request_refund(
            invoice=inv,
            amount_uzs=Decimal("100000.00"),
            payment_id=payment.pk,
        )
        services.register_refund_completion(
            refund.pk,
            payment_id=payment.pk,
            provider="payme",
            provider_refund_id=f"payme-{payment.pk}",
        )

        with pytest.raises(UnprocessableEntity) as exc:
            services.request_refund(
                invoice=inv,
                amount_uzs=Decimal("100000.00"),
                payment_id=payment.pk,
            )
        assert exc.value.code == "payment_not_completed"


def test_in_flight_refund_blocks_a_second_request(tenant_a):
    """Two refund REQUESTS (neither completed yet) cannot both pass against the
    same gross paid amount — the second is rejected as over the net paid."""
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(inv, amount="100000.00", key="refund-in-flight")
        services.allocate_payment(
            payment_id=payment.pk,
            invoice_ids=[inv.pk],
            amount_uzs=Decimal("100000.00"),
        )

        services.request_refund(
            invoice=inv,
            amount_uzs=Decimal("60000.00"),
            payment_id=payment.pk,
        )
        with pytest.raises(ValidationException) as exc:
            services.request_refund(
                invoice=inv,
                amount_uzs=Decimal("60000.00"),
                payment_id=payment.pk,
            )
        assert exc.value.code == "refund_exceeds_paid"


# --------------------------------------------------------------------------- #
# void
# --------------------------------------------------------------------------- #


def test_void_invoice_with_payments_rejected(tenant_a):
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        payment = _captured_payment(inv, amount="100000.00", key="void-with-payment")
        services.allocate_payment(payment_id=payment.pk, amount_uzs=Decimal("100000.00"))
        with pytest.raises(ConflictException):
            services.void_invoice(invoice=inv)


def test_void_clean_invoice(tenant_a):
    with schema_context(tenant_a.schema_name):
        inv = InvoiceFactory(total_uzs=Decimal("100000.00"))
        services.void_invoice(invoice=inv)
        inv.refresh_from_db()
        assert inv.status == Invoice.Status.VOID


# --------------------------------------------------------------------------- #
# outstanding balance — parent scoping
# --------------------------------------------------------------------------- #


def test_outstanding_balance_math(tenant_a):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        InvoiceFactory(student=student, total_uzs=Decimal("100000.00"))
        inv2 = InvoiceFactory(student=student, total_uzs=Decimal("100000.00"))
        # pay one fully
        payment = _captured_payment(inv2, amount="100000.00", key="outstanding-math")
        services.allocate_payment(
            payment_id=payment.pk,
            invoice_ids=[inv2.pk],
            amount_uzs=Decimal("100000.00"),
        )
        assert selectors.outstanding_balance(student.pk) == Decimal("100000.00")


# --------------------------------------------------------------------------- #
# late_payment_reminders — emit once per interval, dedupe same day
# --------------------------------------------------------------------------- #


def test_payment_reminders_emit_once_and_dedupe(tenant_a, django_capture_on_commit_callbacks):
    from datetime import date

    with schema_context(tenant_a.schema_name):
        cache.clear()
        invoice = InvoiceFactory(due_date=date(2020, 1, 1), status=Invoice.Status.ISSUED)
        captured = []
        payment_reminder.connect(lambda **kw: captured.append(kw), weak=False, dispatch_uid="rem")
        try:
            with django_capture_on_commit_callbacks(execute=True):
                n1 = services.emit_payment_reminders(today=date(2020, 1, 10))
            n2 = services.emit_payment_reminders(today=date(2020, 1, 10))
            n3 = services.emit_payment_reminders(today=date(2020, 1, 13))
        finally:
            payment_reminder.disconnect(dispatch_uid="rem")
        assert n1 == 1
        assert n2 == 0  # same interval bucket -> deduped
        assert n3 == 1  # an already-overdue row remains eligible next interval
        assert len(captured) == 2
        assert captured[0]["reminder_cycle"] == "2020-01-01:3:3"
        assert captured[1]["reminder_cycle"] == "2020-01-01:3:4"
        # the invoice flipped to overdue
        assert Invoice.objects.filter(status=Invoice.Status.OVERDUE).count() == 1
        assert AuditLog.objects.filter(
            action=AuditLog.Action.UPDATE,
            resource_type="finance.Invoice",
            resource_id=str(invoice.pk),
            after__status=Invoice.Status.OVERDUE,
        ).exists()


# --------------------------------------------------------------------------- #
# statement task body (weasyprint skip when native libs absent)
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - import probe
    import weasyprint  # noqa: F401

    _HAS_WEASYPRINT = True
except Exception:
    _HAS_WEASYPRINT = False


@pytest.mark.skipif(not _HAS_WEASYPRINT, reason="weasyprint native libs unavailable (CI/Linux runs it)")
def test_render_statement_pdf_real(tenant_a):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        InvoiceFactory(student=student, total_uzs=Decimal("100000.00"))
        pdf = services.render_statement_pdf(student=student, locale="en")
        assert pdf.startswith(b"%PDF")


def test_generate_statement_uploads_to_s3(tenant_a, user_in, monkeypatch):
    """A durable statement row owns the private object uploaded by its worker."""
    from apps.org.models import StaffProfile
    from core.permissions import Role
    from core.role_principals import RolePrincipal

    captured = {}

    def fake_render(*, student, locale="en", invoice_ids=None):
        return b"%PDF-stub"

    def fake_upload(key, data, *, content_type="application/octet-stream"):
        captured["key"] = key
        captured["data"] = data
        return key

    monkeypatch.setattr(services, "render_statement_pdf", fake_render)
    monkeypatch.setattr("infrastructure.storage.s3_client.upload_bytes", fake_upload)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        InvoiceFactory(student=student)
    actor = user_in(tenant_a, roles=[Role.DIRECTOR], branch=student.branch)
    with schema_context(tenant_a.schema_name):
        principal = StaffProfile.objects.create(user=actor, username=f"statement-{actor.pk}")
        export, should_enqueue = services.request_statement_export(
            student_id=student.pk,
            requested_by=actor,
            principal=RolePrincipal(kind="staff", principal_id=principal.pk, user_id=actor.pk),
            locale="en",
        )
        assert should_enqueue is True
        key = services.build_statement_export(export.pk)
        assert key == captured["key"]
        assert key == f"{tenant_a.schema_name}/documents/statements/{export.pk}.pdf"
        assert captured["data"] == b"%PDF-stub"
        export.refresh_from_db()
        assert export.status == export.Status.DONE
