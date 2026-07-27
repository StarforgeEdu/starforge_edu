"""Finance read selectors: eager-loaded, role-scoped queries + balance and
cashier-report aggregates."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import QuerySet, Sum

from apps.finance.models import CashierShift, Invoice
from core.permissions import Role

_ZERO = Decimal("0")

# Director / accountant / cashier see the whole tenant's finance. Parents are
# row-scoped to their guardian-linked children (finance:read_own).
STAFF_ROLES = {Role.DIRECTOR, Role.ACCOUNTANT, Role.CASHIER}

# Statuses that still owe money — outstanding-balance + reminders.
OPEN_STATUSES = (Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE)


def _invoice_base() -> QuerySet[Invoice]:
    return Invoice.objects.select_related(
        "student__user", "cohort", "fee_schedule", "created_by"
    ).prefetch_related("lines", "allocations")


def scoped_invoices(*, user, roles: set[str] | None = None) -> QuerySet[Invoice]:
    """Invoices visible to `user`. Superuser + finance staff -> all; PARENT ->
    guardian-linked children only; STUDENT -> own; everyone else -> none."""
    qs = _invoice_base()
    if getattr(user, "is_superuser", False):
        return qs
    if roles is None:
        roles = {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}
    if roles & STAFF_ROLES:
        return qs
    if Role.PARENT in roles:
        return qs.filter(student__guardians__parent__user=user).distinct()
    if Role.STUDENT in roles:
        return qs.filter(student__user=user)
    return qs.none()


def list_fee_schedules() -> QuerySet:
    from apps.finance.models import FeeSchedule

    return FeeSchedule.objects.select_related("cohort").all()


def list_discounts() -> QuerySet:
    from apps.finance.models import Discount

    return Discount.objects.select_related("student__user", "approved_by").all()


def outstanding_balance(student_id: int) -> Decimal:
    """issued + partially_paid + overdue invoice totals minus allocations, for one
    student. Two aggregate queries, independent of row count."""
    invoices = Invoice.objects.filter(student_id=student_id, status__in=OPEN_STATUSES)
    billed = invoices.aggregate(s=Sum("total_uzs"))["s"] or _ZERO
    allocated = invoices.aggregate(s=Sum("allocations__amount_uzs"))["s"] or _ZERO
    return (Decimal(billed) - Decimal(allocated)).quantize(Decimal("0.01"))


def outstanding_invoices(*, student_id: int, user=None, roles: set[str] | None = None) -> QuerySet[Invoice]:
    """Open invoices for one student, scoped so a parent only sees their own
    children's rows (combine with scoped_invoices to enforce the guardian link)."""
    base = scoped_invoices(user=user, roles=roles) if user is not None else _invoice_base()
    return base.filter(student_id=student_id, status__in=OPEN_STATUSES).order_by("due_date", "id")


def parent_can_see_student(*, user, student_id: int) -> bool:
    """A parent may view a student's balance only when guardian-linked."""
    from apps.parents.models import Guardian

    return Guardian.objects.filter(student_id=student_id, parent__user=user).exists()


def statement_context(*, student) -> dict:
    """Render context for the statement-of-account PDF: every invoice (with lines
    + allocations prefetched) and the outstanding balance for one student."""
    from django.utils import timezone

    invoices = _invoice_base().filter(student=student).order_by("issue_date", "id")
    return {
        "student": student,
        "invoices": list(invoices),
        "outstanding_uzs": outstanding_balance(student.pk),
        "generated_on": timezone.localdate().isoformat(),
    }


def cashier_shift_report(*, shift: CashierShift) -> dict:
    """Per-provider payment totals for a shift + the discrepancy. Tolerates Lane B
    not yet merged (no Payment model) -> zero payments (D3-A-5 acceptance)."""
    totals: dict[str, str] = {}
    payments_total = _ZERO
    try:
        from apps.payments.models import Payment

        rows = (
            Payment.objects.filter(cashier_shift_id=shift.pk, status="completed")
            .values("provider")
            .annotate(total=Sum("amount_uzs"))
        )
        for row in rows:
            amount = Decimal(row["total"] or _ZERO).quantize(Decimal("0.01"))
            totals[row["provider"]] = str(amount)
            payments_total += amount
    except Exception:  # Lane B not merged
        pass

    return {
        "shift_id": shift.pk,
        "cashier_id": shift.cashier_id,
        "branch_id": shift.branch_id,
        "status": shift.status,
        "opened_at": shift.opened_at,
        "closed_at": shift.closed_at,
        "opening_cash_uzs": str(shift.opening_cash_uzs),
        "closing_cash_uzs": str(shift.closing_cash_uzs) if shift.closing_cash_uzs is not None else None,
        "discrepancy_uzs": str(shift.discrepancy_uzs) if shift.discrepancy_uzs is not None else None,
        "payments_total_uzs": str(payments_total.quantize(Decimal("0.01"))),
        "totals_by_provider": totals,
    }
