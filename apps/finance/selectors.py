"""Finance read selectors: eager-loaded, role-scoped queries + balance and
cashier-report aggregates."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import DecimalField, OuterRef, Q, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from apps.finance.models import CashierShift, Invoice, PaymentAllocation
from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES
from core.permissions import (
    PermissionRoleSet,
    Role,
    get_unambiguous_user_roles,
    has_permission_code,
)
from core.scoping import (
    permission_membership_is_unscoped,
    permission_membership_scope_q,
    permission_membership_scopes,
)

_ZERO = Decimal("0")

# Organization-wide visibility comes only from the same active staff membership
# that grants the requested permission. Other staff grants retain their exact
# branch/department boundary; parents and students retain natural own-record
# scopes only when the same account-kind membership grants ``finance:read_own``.

# Statuses that still owe money — outstanding-balance + reminders.
OPEN_STATUSES = (Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE)


def _invoice_related() -> QuerySet[Invoice]:
    return Invoice.objects.select_related(
        "student__user",
        "student__current_cohort",
        "cohort",
        "fee_schedule",
        "created_by",
        "branch_at_issue",
        "department_at_issue",
    )


def _invoice_base() -> QuerySet[Invoice]:
    """Detail/statement query with nested records eagerly loaded."""
    return _invoice_related().prefetch_related("lines", "allocations")


def _invoice_summary_base() -> QuerySet[Invoice]:
    """List query: one row per invoice, no nested collection prefetches.

    A correlated aggregate avoids both the two full prefetch result sets and the
    multiplication bug a direct JOIN/SUM would have under guardian/cohort scope
    joins.  ``allocated_uzs`` is consumed by the lightweight presenter.
    """
    money_field = DecimalField(max_digits=24, decimal_places=2)
    allocation_total = (
        PaymentAllocation.objects.filter(invoice_id=OuterRef("pk"))
        .values("invoice_id")
        .annotate(total=Sum("amount_uzs"))
        .values("total")[:1]
    )
    return _invoice_related().annotate(
        allocated_uzs=Coalesce(
            Subquery(allocation_total, output_field=money_field),
            Value(_ZERO, output_field=money_field),
            output_field=money_field,
        )
    )


def _scope_invoices(
    *,
    qs: QuerySet[Invoice],
    user,
    roles: set[str] | None,
    permission: str,
) -> QuerySet[Invoice]:
    """Apply the shared person/permission scope to either list or detail rows."""
    # Historical ownership is authoritative only after write-time capture or a
    # reviewed backfill. Quarantined/ambiguous rows stay out of every product
    # read and are visible only to the backfill/review tooling.
    qs = qs.filter(attribution_status__in=ATTRIBUTED_SCOPE_STATUSES)
    if getattr(user, "is_superuser", False):
        return qs
    if roles is None:
        roles = get_unambiguous_user_roles(user)
    if isinstance(roles, PermissionRoleSet):
        if permission_membership_is_unscoped(
            roles=roles,
            permission=permission,
            account_kinds={"staff"},
        ):
            return qs
    elif Role.DIRECTOR in roles:
        # Raw role sets are retained only for explicit legacy selector callers.
        return qs
    visible = Q(pk__in=[])
    if has_permission_code(roles, permission):
        if isinstance(roles, PermissionRoleSet):
            visible |= permission_membership_scope_q(
                roles=roles,
                permission=permission,
                branch_field="branch_at_issue_id",
                department_field="department_at_issue_id",
                account_kinds={"staff"},
            )
        else:
            # Direct selector calls with a raw legacy role set retain the old
            # compatibility behavior; request paths use PermissionRoleSet.
            allowed_branches = (
                user.role_memberships.filter(
                    role__in=roles,
                    revoked_at__isnull=True,
                    branch_id__isnull=False,
                )
                .filter(Q(account_type__isnull=True) | Q(account_type__is_active=True))
                .values_list("branch_id", flat=True)
            )
            visible |= Q(branch_at_issue_id__in=allowed_branches)
    if permission == "finance:read" and has_natural_finance_scope(
        roles,
        account_kind="parent",
        legacy_role=Role.PARENT,
    ):
        visible |= Q(
            student__guardians__parent__user=user,
            student__guardians__revoked_at__isnull=True,
        )
    if permission == "finance:read" and has_natural_finance_scope(
        roles,
        account_kind="student",
        legacy_role=Role.STUDENT,
    ):
        visible |= Q(student__user=user)
    return qs.filter(visible).distinct()


def has_natural_finance_scope(
    roles: set[str],
    *,
    account_kind: str,
    legacy_role: str,
) -> bool:
    """Require own-finance authority and natural identity on one membership.

    A custom staff assignment carrying ``finance:read_own`` must not borrow a
    parent/student identity from an unrelated assignment. Plain role sets are
    accepted only for explicit legacy selector callers and retain their
    historical role-name behavior.
    """
    if isinstance(roles, PermissionRoleSet):
        return bool(
            permission_membership_scopes(
                roles=roles,
                permission="finance:read_own",
                account_kinds={account_kind},
            )
        )
    return legacy_role in roles


def scoped_invoices(
    *,
    user,
    roles: set[str] | None = None,
    permission: str = "finance:read",
) -> QuerySet[Invoice]:
    """Invoices visible through exact permission-bearing or natural scopes."""
    return _scope_invoices(qs=_invoice_base(), user=user, roles=roles, permission=permission)


def scoped_invoice_summaries(
    *,
    user,
    roles: set[str] | None = None,
    permission: str = "finance:read",
) -> QuerySet[Invoice]:
    """Visible invoices optimized for a paginated register/list response."""
    return _scope_invoices(
        qs=_invoice_summary_base(),
        user=user,
        roles=roles,
        permission=permission,
    )


def list_fee_schedules() -> QuerySet:
    from apps.finance.models import FeeSchedule

    return FeeSchedule.objects.select_related("cohort").all()


def list_discounts() -> QuerySet:
    from apps.finance.models import Discount

    return Discount.objects.select_related("student__user", "approved_by").all()


def outstanding_balance(student_id: int) -> Decimal:
    """issued + partially_paid + overdue invoice totals minus allocations, for one
    student. Two aggregate queries, independent of row count."""
    invoices = Invoice.objects.filter(
        student_id=student_id,
        status__in=OPEN_STATUSES,
        attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
    )
    return outstanding_balance_for_invoices(invoices)


def outstanding_balance_for_invoices(invoices: QuerySet[Invoice]) -> Decimal:
    """Calculate a balance from only the supplied visible invoice queryset.

    Rebase through a primary-key subquery before aggregating. Scoped invoice
    querysets may join guardians or membership relations and use ``distinct``;
    aggregating their joins directly can multiply both billed and allocated
    amounts. This form preserves the authorization boundary and one-row invoice
    cardinality.
    """
    visible_ids = invoices.order_by().values("pk")
    scoped = Invoice.objects.filter(pk__in=Subquery(visible_ids))
    billed = scoped.aggregate(s=Sum("total_uzs"))["s"] or _ZERO
    allocated = scoped.aggregate(s=Sum("allocations__amount_uzs"))["s"] or _ZERO
    return (Decimal(billed) - Decimal(allocated)).quantize(Decimal("0.01"))


def outstanding_invoices(*, student_id: int, user=None, roles: set[str] | None = None) -> QuerySet[Invoice]:
    """Open invoices for one student, scoped so a parent only sees their own
    children's rows (combine with scoped_invoices to enforce the guardian link)."""
    base = scoped_invoices(user=user, roles=roles) if user is not None else _invoice_base()
    return base.filter(student_id=student_id, status__in=OPEN_STATUSES).order_by("due_date", "id")


def parent_can_see_student(*, user, student_id: int) -> bool:
    """A parent may view a student's balance only when guardian-linked."""
    from apps.parents.models import Guardian

    return Guardian.objects.filter(
        student_id=student_id,
        parent__user=user,
        revoked_at__isnull=True,
    ).exists()


def statement_context(*, student, invoice_ids: list[int] | None = None) -> dict:
    """Render context for the statement-of-account PDF: every invoice (with lines
    + allocations prefetched) and its scope-matched outstanding balance."""
    from django.utils import timezone

    invoices_qs = (
        _invoice_base()
        .filter(student=student, attribution_status__in=ATTRIBUTED_SCOPE_STATUSES)
        .order_by("issue_date", "id")
    )
    if invoice_ids is not None:
        invoices_qs = invoices_qs.filter(pk__in=invoice_ids)
    open_invoices = invoices_qs.filter(status__in=OPEN_STATUSES)
    billed = open_invoices.aggregate(s=Sum("total_uzs"))["s"] or _ZERO
    allocated = open_invoices.aggregate(s=Sum("allocations__amount_uzs"))["s"] or _ZERO
    return {
        "student": student,
        "invoices": list(invoices_qs),
        "outstanding_uzs": (Decimal(billed) - Decimal(allocated)).quantize(Decimal("0.01")),
        "generated_on": timezone.localdate().isoformat(),
    }


def cashier_shift_report(*, shift: CashierShift) -> dict:
    """Per-provider payment totals for a shift plus its discrepancy."""
    totals: dict[str, str] = {}
    payments_total = _ZERO
    from apps.payments.models import Payment

    rows = (
        Payment.objects.filter(
            cashier_shift_id=shift.pk,
            status="completed",
            attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
        )
        .values("provider")
        .annotate(total=Sum("amount_uzs"))
    )
    for row in rows:
        amount = Decimal(row["total"] or _ZERO).quantize(Decimal("0.01"))
        totals[row["provider"]] = str(amount)
        payments_total += amount

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
