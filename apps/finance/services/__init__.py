"""Finance write services (TASKS §15, TD-13/14/18).

All writes flow through here: typed, keyword-only, `@transaction.atomic`, raising
`StarforgeError` subclasses and emitting signals via `transaction.on_commit`.

Highlights:
- `issue_invoice` — per-center `INV-{YYYY}-{seq:06d}` numbering, FX snapshot frozen
  at issue, sibling-discount materialization (when `CenterSettings
  .sibling_discount_percent > 0` and the student shares a Guardian with another
  enrolled student).
- `auto_issue_on_enrollment` — idempotent receiver body; dedupe on
  `(student, fee_schedule, period)`.
- `allocate_payment` — oldest-due-first split, EXACT Decimal accounting,
  over-allocation -> `ValidationException`.
- cashier shift open/close + statement (weasyprint, off-request, S3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.cohorts.models import CohortMembership
from apps.finance.models import (
    CashierShift,
    Discount,
    Expense,
    FeeSchedule,
    Invoice,
    InvoiceLine,
    PaymentAllocation,
    PaymentPlan,
    PaymentPlanInstallment,
    Refund,
)
from apps.finance.signals import invoice_issued
from apps.org.selectors import get_center_settings
from apps.students.models import StudentProfile
from core.exceptions import (
    ConflictException,
    NotFoundException,
    PermissionException,
    UnprocessableEntity,
    ValidationException,
)
from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES, ScopeAttributionStatus
from core.timezones import organization_timezone_context
from core.utils import current_schema, stable_hash

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")
_FX_QUANTUM = Decimal("0.0001")
# DecimalField(max_digits=12, decimal_places=4): eight integer digits maximum.
_FX_MAX_EXCLUSIVE = Decimal("100000000")
_MONEY_MAX_EXCLUSIVE = Decimal("10000000000000000")
_MAX_INVOICE_LINES = 500

# Statuses that still owe money (used for allocation + reminders + balance).
OPEN_STATUSES = (Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE)


# ---------------------------------------------------------------------------
# Invoice numbering + FX
# ---------------------------------------------------------------------------


def _next_invoice_number(*, year: int) -> str:
    """`INV-{YYYY}-{seq:06d}`, unique per center (the tenant schema).

    Serialized on a Postgres transaction-level advisory lock keyed by
    (schema, year). A `MAX()+1`-style `select_for_update` locks no rows when the
    year is empty (or after a gap), so two concurrent first issues would both
    compute seq=1 and the unique constraint would surface as a 500. The advisory
    lock exists regardless of row count, so two concurrent `issue_invoice`
    transactions serialize here and always get distinct sequence numbers; the
    lock auto-releases at COMMIT/ROLLBACK (it must be taken inside an atomic
    block — `issue_invoice` is `@transaction.atomic`).
    """
    from django.db import connection

    # 64-bit advisory lock key derived from (schema, year). pg_advisory_xact_lock
    # takes two int4s; we split a stable 64-bit hash so distinct (schema, year)
    # pairs serialize independently.
    digest = stable_hash(f"invoice_number:{current_schema()}:{year}")
    key = int(digest[:16], 16)
    key_hi = (key >> 32) & 0xFFFFFFFF
    key_lo = key & 0xFFFFFFFF
    # Map the unsigned halves into signed int4 range Postgres expects.
    key_hi = key_hi - 0x100000000 if key_hi >= 0x80000000 else key_hi
    key_lo = key_lo - 0x100000000 if key_lo >= 0x80000000 else key_lo
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [key_hi, key_lo])

    prefix = f"INV-{year}-"
    last = Invoice.objects.filter(number__startswith=prefix).order_by("-number").first()
    seq = (int(last.number.rsplit("-", 1)[1]) + 1) if last else 1
    return f"{prefix}{seq:06d}"


def _fx_snapshot(settings) -> tuple[Decimal | None, str]:
    """Return `(fx_rate_usd, fx_source)` frozen onto the invoice.

    `CenterSettings.fx_source` selects the strategy:
    - "manual" -> `CenterSettings.fx_rate_usd_manual` (a fixed admin-entered rate);
    - "cbu" (default) -> the cached CBU rate written by the FX-refresh task; falls
      back to the manual rate when no cached rate exists (mock-first, TD-2).
    A null rate means USD totals are simply not computed (never a hard failure).
    """
    source = settings.fx_source or "cbu"
    manual = normalize_fx_rate(getattr(settings, "fx_rate_usd_manual", None))
    if source == "manual":
        return manual, "manual"
    # "cbu": use the cached rate if present, else the manual fallback.
    cached = _cached_cbu_rate()
    return (cached if cached is not None else manual), source


def _cached_cbu_rate() -> Decimal | None:
    """Per-tenant cached CBU UZS->USD rate (written by `refresh_fx_rates`). Mock
    path returns None until the task populates it; manual fallback covers issue."""
    from django.core.cache import cache

    raw = cache.get(f"finance:fx_rate_usd:{current_schema()}")
    return normalize_fx_rate(raw)


def normalize_fx_rate(raw: Any) -> Decimal | None:
    """Return a finite, positive, model-safe UZS-per-USD rate."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        rate = Decimal(str(raw))
    except (ValueError, ArithmeticError):
        return None
    if not rate.is_finite() or rate <= _ZERO or rate >= _FX_MAX_EXCLUSIVE:
        return None
    try:
        normalized = rate.quantize(_FX_QUANTUM)
    except ArithmeticError:
        return None
    return normalized if normalized > _ZERO else None


def _usd_total(total_uzs: Decimal, rate: Decimal | None) -> Decimal | None:
    rate = normalize_fx_rate(rate)
    if rate is None:
        return None
    try:
        total = (total_uzs / rate).quantize(_CENT)
    except ArithmeticError:
        return None
    if not total.is_finite() or abs(total) >= _MONEY_MAX_EXCLUSIVE:
        return None
    return total


def _invoice_line_amount(quantity: Decimal, unit_price: Decimal) -> Decimal:
    """Multiply a line without leaking decimal/column overflow as a database 500."""
    if not quantity.is_finite() or not unit_price.is_finite():
        raise ValidationException(
            _("Invoice line values must be finite numbers."),
            code="invalid_invoice_line",
            fields={"lines": ["non_finite"]},
        )
    try:
        amount = (quantity * unit_price).quantize(_CENT)
    except ArithmeticError as exc:
        raise ValidationException(
            _("Invoice line amount is too large."),
            code="invoice_amount_too_large",
            fields={"lines": ["amount_too_large"]},
        ) from exc
    if abs(amount) >= _MONEY_MAX_EXCLUSIVE:
        raise ValidationException(
            _("Invoice line amount is too large."),
            code="invoice_amount_too_large",
            fields={"lines": ["amount_too_large"]},
        )
    return amount


# ---------------------------------------------------------------------------
# Discounts (sibling materialization)
# ---------------------------------------------------------------------------


def _has_enrolled_sibling(student: StudentProfile) -> bool:
    """True when `student` shares an active `Guardian` with another ENROLLED/ACTIVE
    student. Routed via parents.Guardian (the sanctioned parent<->student link)."""
    parent_ids = list(student.guardians.filter(revoked_at__isnull=True).values_list("parent_id", flat=True))
    if not parent_ids:
        return False
    return (
        StudentProfile.objects.filter(
            guardians__parent_id__in=parent_ids,
            guardians__revoked_at__isnull=True,
            status__in=(StudentProfile.Status.ENROLLED, StudentProfile.Status.ACTIVE),
        )
        .exclude(pk=student.pk)
        .exists()
    )


def _active_discounts(student: StudentProfile, *, on: date) -> list[Discount]:
    return list(
        Discount.objects.filter(student=student, is_active=True).filter(
            (
                # valid_from null OR <= on
                models_q_lte_or_null("valid_from", on)
            )
            & models_q_gte_or_null("valid_until", on)
        )
    )


def models_q_lte_or_null(field: str, value):
    from django.db.models import Q

    return Q(**{f"{field}__isnull": True}) | Q(**{f"{field}__lte": value})


def models_q_gte_or_null(field: str, value):
    from django.db.models import Q

    return Q(**{f"{field}__isnull": True}) | Q(**{f"{field}__gte": value})


def _discount_amount(discount: Discount, base_uzs: Decimal) -> Decimal:
    """The positive UZS value of a discount against `base_uzs` (capped at base)."""
    if discount.percent is not None:
        amount = (base_uzs * discount.percent / _HUNDRED).quantize(_CENT)
    else:
        amount = (discount.fixed_amount_uzs or _ZERO).quantize(_CENT)
    return min(amount, base_uzs)


# ---------------------------------------------------------------------------
# issue_invoice
# ---------------------------------------------------------------------------


@transaction.atomic
def issue_invoice(
    *,
    student_id: int,
    fee_schedule_id: int | None = None,
    lines: list[dict] | None = None,
    period: str = "",
    created_by=None,
    apply_discounts: bool = True,
    allowed_scope_pairs: set[tuple[int, int | None]] | None = None,
) -> Invoice:
    """Issue an invoice for `student_id`.

    Lines come from `fee_schedule_id` (one tuition line of `amount_uzs`) and/or an
    explicit `lines=[{description, line_type, quantity, unit_price_uzs}]` list.
    Standing discounts (including an auto-materialized sibling discount when
    `CenterSettings.sibling_discount_percent > 0` and the student has an enrolled
    sibling) are appended as negative discount lines. The invoice number,
    `fx_rate_usd`, and `total_usd` are frozen at issue.

    `apply_discounts=False` issues the invoice WITHOUT any standing discount lines —
    used for a penalty/fine charge, where a scholarship must not shrink a punishment.
    """
    # Student placement is the source of the historical ownership snapshot. Lock
    # it for the whole issuance transaction so a concurrent transfer cannot race
    # between authorization and persistence. Only lock the student row: the
    # current-cohort relation is nullable, so asking PostgreSQL to lock every
    # row brought in by select_related() would apply FOR UPDATE to the nullable
    # side of an outer join and reject invoice issuance for unassigned students.
    student = (
        StudentProfile.objects.select_for_update(of=("self",))
        .select_related("user", "current_cohort")
        .filter(pk=student_id)
        .first()
    )
    if student is None:
        raise NotFoundException(_("Student not found."), code="student_not_found")
    invoice_cohort = student.current_cohort
    if invoice_cohort is not None and invoice_cohort.branch_id != student.branch_id:
        raise ConflictException(
            _("The student's branch and cohort attribution conflict."),
            code="student_scope_conflict",
        )

    settings = get_center_settings()
    fee_schedule = None
    if fee_schedule_id is not None:
        fee_schedule = FeeSchedule.objects.select_related("cohort").filter(pk=fee_schedule_id).first()
        if fee_schedule is None:
            raise NotFoundException(_("Fee schedule not found."), code="fee_schedule_not_found")
        schedule_cohort = fee_schedule.cohort
        if schedule_cohort is not None and schedule_cohort.branch_id != student.branch_id:
            raise ValidationException(
                _("The fee schedule belongs to another student branch."),
                code="fee_schedule_branch_mismatch",
                fields={"fee_schedule": ["branch_mismatch"]},
            )
        if schedule_cohort is not None:
            # ``current_cohort`` is only the student's stable primary. A valid
            # secondary enrollment is represented by another active membership
            # and may carry its own fee schedule. The student row lock above is
            # also taken by enroll/move/unenroll, so this membership decision and
            # the immutable invoice snapshot cannot race a cohort transition.
            if (
                schedule_cohort.pk != student.current_cohort_id
                and not CohortMembership.objects.filter(
                    student_id=student.pk,
                    cohort_id=schedule_cohort.pk,
                    end_date__isnull=True,
                ).exists()
            ):
                raise ValidationException(
                    _("The fee schedule belongs to another student cohort."),
                    code="fee_schedule_cohort_mismatch",
                    fields={"fee_schedule": ["cohort_mismatch"]},
                )
            # Attribute a course-specific charge to the cohort that incurred it,
            # not to an unrelated primary cohort. This value and its branch /
            # department snapshots remain frozen after issuance.
            invoice_cohort = schedule_cohort

    invoice_department_id = invoice_cohort.department_id if invoice_cohort is not None else None
    if allowed_scope_pairs is not None and not any(
        allowed_branch_id == student.branch_id
        and (allowed_department_id is None or allowed_department_id == invoice_department_id)
        for allowed_branch_id, allowed_department_id in allowed_scope_pairs
    ):
        # HTTP callers pass only the exact memberships that grant finance:write.
        # Rechecking after the student row is locked closes the transfer race
        # between the view's preliminary lookup and this immutable snapshot.
        raise PermissionException(code="out_of_scope")

    line_specs: list[dict] = []
    if fee_schedule is not None:
        line_specs.append(
            {
                "description": fee_schedule.name,
                "line_type": InvoiceLine.LineType.TUITION,
                "quantity": Decimal("1"),
                "unit_price_uzs": fee_schedule.amount_uzs,
            }
        )
    explicit_lines = lines or []
    if len(explicit_lines) > _MAX_INVOICE_LINES:
        raise ValidationException(
            _("An invoice has too many line items."),
            code="too_many_invoice_lines",
            fields={"lines": [f"max_items:{_MAX_INVOICE_LINES}"]},
        )
    for raw in explicit_lines:
        qty = Decimal(str(raw.get("quantity", "1")))
        line_specs.append(
            {
                "description": raw["description"],
                "line_type": raw.get("line_type", InvoiceLine.LineType.OTHER),
                "quantity": qty,
                "unit_price_uzs": Decimal(str(raw["unit_price_uzs"])),
            }
        )
    if not line_specs:
        raise ValidationException(
            _("An invoice needs at least a fee schedule or one explicit line."),
            code="empty_invoice",
        )

    charge_lines = [
        {
            **spec,
            "amount_uzs": _invoice_line_amount(spec["quantity"], spec["unit_price_uzs"]),
        }
        for spec in line_specs
    ]
    gross = sum((line["amount_uzs"] for line in charge_lines), _ZERO)
    if abs(gross) >= _MONEY_MAX_EXCLUSIVE:
        raise ValidationException(
            _("Invoice total is too large."),
            code="invoice_amount_too_large",
            fields={"lines": ["total_too_large"]},
        )

    today = timezone.localdate()
    discount_lines = (
        _build_discount_lines(student=student, base_uzs=gross, on=today, settings=settings)
        if apply_discounts
        else []
    )
    total_uzs = (gross + sum((line["amount_uzs"] for line in discount_lines), _ZERO)).quantize(_CENT)
    if total_uzs < _ZERO:
        total_uzs = _ZERO

    rate, fx_source = _fx_snapshot(settings)
    invoice = Invoice.objects.create(
        number=_next_invoice_number(year=today.year),
        student=student,
        cohort=invoice_cohort,
        branch_at_issue_id=student.branch_id,
        department_at_issue_id=invoice_department_id,
        attribution_status=ScopeAttributionStatus.CAPTURED,
        fee_schedule=fee_schedule,
        period=period,
        status=Invoice.Status.ISSUED,
        issue_date=today,
        due_date=_due_date(today, fee_schedule, settings),
        # Every persisted amount/line/discount/fee column in finance v1 is
        # explicitly named ``*_uzs`` and is calculated in UZS. Relabelling the
        # same numeric value with a configurable presentation currency would be
        # a silent money error. Multi-currency requires a versioned ledger
        # migration and explicit conversion; v1 stays UZS.
        currency="UZS",
        total_uzs=total_uzs,
        fx_rate_usd=rate,
        fx_source=fx_source,
        total_usd=_usd_total(total_uzs, rate),
        created_by=created_by,
    )
    InvoiceLine.objects.bulk_create(
        InvoiceLine(invoice=invoice, **line) for line in (charge_lines + discount_lines)
    )

    schema = current_schema()
    transaction.on_commit(
        lambda: invoice_issued.send(
            sender=Invoice,
            invoice_id=invoice.pk,
            student_id=student.pk,
            schema_name=schema,
        )
    )
    return invoice


def _build_discount_lines(*, student, base_uzs: Decimal, on: date, settings) -> list[dict]:
    """Negative discount lines to materialize: standing discounts + an auto
    sibling discount when configured and the student has an enrolled sibling."""
    lines: list[dict] = []
    # Aggregate discount is capped at the charge (base_uzs): stacking discounts that
    # sum beyond 100% must floor the bill at zero, NOT drive the persisted lines below
    # zero. Without the cap, total_uzs clamps to 0 while sum(InvoiceLine) goes negative,
    # breaking the sum(lines) == total_uzs invariant (silent negative-balance corruption).
    remaining = base_uzs
    for discount in _active_discounts(student, on=on):
        if remaining <= _ZERO:
            break  # invoice already fully discounted — further discounts can't apply
        amount = _discount_amount(discount, base_uzs)
        if amount <= _ZERO:
            continue
        amount = min(amount, remaining)  # cap so cumulative discount never exceeds the charge
        # F23-1: a one-time credit (an absence deduction) credits exactly ONE invoice, then
        # retires — so a single missed lesson is never credited twice. Claim it with a
        # conditional UPDATE (short-circuited so it only runs for single_use discounts): it
        # matches 0 rows — and we skip the line — if a concurrent invoice already consumed
        # it, since the row lock serialises the two issuers inside issue_invoice's
        # transaction. So it can't double-credit even under concurrent issuance. The claim
        # runs only once we KNOW a positive amount applies (after the cap), so a credit is
        # never consumed on an already-zeroed invoice.
        if discount.single_use and not Discount.objects.filter(pk=discount.pk, is_active=True).update(
            is_active=False
        ):
            continue
        remaining -= amount
        lines.append(
            {
                "description": str(_("Discount: %(t)s")) % {"t": discount.get_discount_type_display()},
                "line_type": InvoiceLine.LineType.DISCOUNT,
                "quantity": Decimal("1"),
                "unit_price_uzs": -amount,
                "amount_uzs": -amount,
            }
        )

    sibling_pct = getattr(settings, "sibling_discount_percent", None) or _ZERO
    if remaining > _ZERO and sibling_pct > _ZERO and _has_enrolled_sibling(student):
        amount = min((base_uzs * Decimal(str(sibling_pct)) / _HUNDRED).quantize(_CENT), remaining)
        if amount > _ZERO:
            lines.append(
                {
                    "description": str(_("Sibling discount (%(p)s%%)")) % {"p": sibling_pct},
                    "line_type": InvoiceLine.LineType.DISCOUNT,
                    "quantity": Decimal("1"),
                    "unit_price_uzs": -amount,
                    "amount_uzs": -amount,
                }
            )
    return lines


def _due_date(issue_day: date, fee_schedule: FeeSchedule | None, settings) -> date:
    """Due date for a freshly issued invoice: the fee schedule's
    `due_day_of_month` this month (or next month if already past)."""
    import calendar

    # Clamp the lower bound: a stored due_day_of_month of 0 (legacy rows / other
    # write paths) would make date(year, month, 0) raise ValueError -> 500. The
    # upper bound is handled per-month by min(day, last_day) below.
    day = max(fee_schedule.due_day_of_month if fee_schedule else 5, 1)
    year, month = issue_day.year, issue_day.month
    last_day = calendar.monthrange(year, month)[1]
    due = date(year, month, min(day, last_day))
    if due < issue_day:
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        last_day = calendar.monthrange(year, month)[1]
        due = date(year, month, min(day, last_day))
    return due


@transaction.atomic
def void_invoice(*, invoice: Invoice, actor=None) -> Invoice:
    # Re-fetch under a row lock (like extend/restore_invoice_due_date) so a concurrent
    # allocate_payment — which locks the invoice — can't slip a PaymentAllocation in
    # between the guard checks and the VOID write. Without the lock, void reads a
    # pre-allocation snapshot (status not PAID, no allocations), then its UPDATE lands
    # AFTER allocate commits, stamping VOID over a just-paid invoice and orphaning a
    # live payment on a voided bill (check-then-act race).
    locked = Invoice.objects.select_for_update().filter(pk=invoice.pk).first()
    if locked is None:
        raise NotFoundException(_("Invoice not found."), code="invoice_not_found")
    if locked.status == Invoice.Status.PAID:
        raise ConflictException(_("A paid invoice cannot be voided."), code="invoice_paid")
    if locked.allocations.exists():
        raise ConflictException(
            _("An invoice with payments cannot be voided; refund first."), code="invoice_has_payments"
        )
    locked.status = Invoice.Status.VOID
    locked.save(update_fields=["status", "updated_at"])
    return locked


@transaction.atomic
def extend_invoice_due_date(*, invoice_id: int, new_due_date: date, actor=None) -> Invoice:
    """Push an open invoice's due date later — a sanctioned payment delay whose
    sign-off lives in the Approvals engine (the `payment_delay` KIND). You can
    only delay: `new_due_date` must be strictly after the current one, never
    advance or backdate (anti-fraud). If the invoice had tipped OVERDUE and the
    new deadline is today or later, it is un-overdued — status recomputes to
    issued / partially_paid from its payments so it leaves the dunning queue."""
    invoice = Invoice.objects.select_for_update().filter(pk=invoice_id).first()
    if invoice is None:
        raise NotFoundException(_("Invoice not found."), code="invoice_not_found")
    if invoice.status not in OPEN_STATUSES:
        raise UnprocessableEntity(
            _("Only an open invoice can have its due date extended."), code="invoice_not_open"
        )
    if invoice.due_date is None:
        raise UnprocessableEntity(_("This invoice has no due date to extend."), code="invoice_no_due_date")
    if new_due_date <= invoice.due_date:
        raise UnprocessableEntity(
            _("A payment delay can only move the due date later."), code="due_date_not_later"
        )
    invoice.due_date = new_due_date
    invoice.save(update_fields=["due_date", "updated_at"])
    # Recompute unconditionally. A still-past extension remains overdue; an
    # extension to today-or-later leaves the dunning queue immediately.
    _refresh_invoice_status(invoice)
    return invoice


@transaction.atomic
def restore_invoice_due_date(*, invoice_id: int, due_date: date | None, actor=None) -> Invoice:
    """Undo a payment-delay extension: put the due date back and recompute status
    (re-flagging OVERDUE when the restored date is now past and the bill still
    owes money). Used to reverse the effect when an approved payment_delay is
    later rejected. A voided invoice is left untouched."""
    invoice = Invoice.objects.select_for_update().filter(pk=invoice_id).first()
    if invoice is None:
        raise NotFoundException(_("Invoice not found."), code="invoice_not_found")
    if invoice.status == Invoice.Status.VOID:
        return invoice
    invoice.due_date = due_date
    invoice.save(update_fields=["due_date", "updated_at"])
    _refresh_invoice_status(invoice)
    return invoice


# ---------------------------------------------------------------------------
# Auto-issue on enrollment (D3-A-3)
# ---------------------------------------------------------------------------


def _period_key(*, on: date | None = None) -> str:
    on = on or timezone.localdate()
    return on.strftime("%Y-%m")


@transaction.atomic
def auto_issue_on_enrollment(*, student_id: int, cohort_id: int | None = None) -> Invoice | None:
    """Idempotent: issue one invoice for the student's matching active
    `FeeSchedule` (cohort-specific if present, else the center-wide default).
    Dedupes on `(student, fee_schedule, period)` — re-firing the enrollment signal
    creates nothing new. Returns the invoice (existing or created), or None when
    no active fee schedule matches."""
    # Serialize the dedupe read with invoice issuance and every cohort membership
    # transition. Without this lock, two enrollment callbacks can both observe no
    # invoice before one reaches issue_invoice and the database uniqueness guard
    # turns an otherwise idempotent retry into an IntegrityError.
    student = StudentProfile.objects.select_for_update(of=("self",)).filter(pk=student_id).first()
    if student is None:
        return None

    cohort_id = cohort_id or student.current_cohort_id
    fee_schedule = (
        FeeSchedule.objects.filter(is_active=True, cohort_id=cohort_id).order_by("id").first()
        if cohort_id
        else None
    )
    if fee_schedule is None:  # fall back to the center-wide default (cohort is null)
        fee_schedule = FeeSchedule.objects.filter(is_active=True, cohort__isnull=True).order_by("id").first()
    if fee_schedule is None:
        return None

    period = _period_key()
    existing = Invoice.objects.filter(student=student, fee_schedule=fee_schedule, period=period).first()
    if existing is not None:
        return existing
    return issue_invoice(student_id=student.pk, fee_schedule_id=fee_schedule.pk, period=period)


# ---------------------------------------------------------------------------
# allocate_payment (D3-A-4)
# ---------------------------------------------------------------------------


def _outstanding_for(invoice: Invoice) -> Decimal:
    allocated = invoice.allocations.aggregate(s=Sum("amount_uzs"))["s"] or _ZERO
    return (invoice.total_uzs - allocated).quantize(_CENT)


def _refresh_invoice_status(invoice: Invoice) -> None:
    """Derive the complete invoice state from balance and business date.

    Every allocation and reversal caller holds the invoice row lock. Keeping the
    overdue decision here means a partial payment or refund cannot temporarily
    erase delinquency until the next reminder job. The organization timezone is
    resolved explicitly because services also run outside request middleware
    (management commands, signals, and direct task calls).
    """
    if invoice.status == Invoice.Status.VOID:
        return
    allocated = invoice.allocations.aggregate(s=Sum("amount_uzs"))["s"] or _ZERO
    if allocated >= invoice.total_uzs and invoice.total_uzs > _ZERO:
        new_status = Invoice.Status.PAID
    elif invoice.due_date is not None:
        with organization_timezone_context():
            today = timezone.localdate()
        new_status = (
            Invoice.Status.OVERDUE
            if invoice.due_date < today
            else (Invoice.Status.PARTIALLY_PAID if allocated > _ZERO else Invoice.Status.ISSUED)
        )
    elif allocated > _ZERO:
        new_status = Invoice.Status.PARTIALLY_PAID
    else:
        new_status = Invoice.Status.ISSUED
    if invoice.status != new_status:
        invoice.status = new_status
        invoice.save(update_fields=["status", "updated_at"])


def _locked_attributed_payment(payment_id: int):
    """Lock a payment whose immutable historical attribution is usable."""
    from apps.payments.models import Payment

    payment = (
        Payment.objects.select_for_update()
        .only(
            "id",
            "amount_uzs",
            "status",
            "branch_at_payment_id",
            "department_at_payment_id",
            "attribution_status",
            "metadata",
        )
        .filter(pk=payment_id)
        .first()
    )
    if payment is None:
        raise NotFoundException(_("Payment not found."), code="payment_not_found")
    if payment.branch_at_payment_id is None or payment.attribution_status not in ATTRIBUTED_SCOPE_STATUSES:
        raise ValidationException(
            _("The payment's historical ownership must be reviewed before allocation."),
            code="payment_attribution_unavailable",
        )
    return payment


def _assert_payment_completed(payment) -> None:
    from apps.payments.models import Payment

    if payment.status != Payment.Status.COMPLETED:
        raise UnprocessableEntity(
            _("Only completed payments can change invoice balances."),
            code="payment_not_completed",
        )


def _locked_payment_scope(payment_id: int) -> tuple[int, int | None]:
    """Lock and return a payment's immutable historical branch/department."""
    payment = _locked_attributed_payment(payment_id)
    _assert_payment_completed(payment)
    return payment.branch_at_payment_id, payment.department_at_payment_id


def _assert_allocation_scope(
    *,
    invoices,
    payment_scope: tuple[int, int | None],
) -> None:
    """Prevent one payment from crossing its captured branch/department scope."""
    payment_branch_id, payment_department_id = payment_scope
    for invoice in invoices:
        if (
            invoice.attribution_status not in ATTRIBUTED_SCOPE_STATUSES
            or invoice.branch_at_issue_id != payment_branch_id
            or invoice.department_at_issue_id != payment_department_id
        ):
            raise ValidationException(
                _("A payment can only be allocated within its historical scope."),
                code="allocation_scope_mismatch",
            )


def _allocation_intent(
    *,
    mode: str,
    amount: Decimal,
    invoice_amounts: dict[int, Decimal] | None = None,
    invoice_ids: set[int] | None = None,
) -> str:
    if invoice_amounts is not None:
        targets = ",".join(
            f"{invoice_id}:{invoice_amounts[invoice_id].quantize(_CENT)}"
            for invoice_id in sorted(invoice_amounts)
        )
    elif invoice_ids is not None:
        targets = ",".join(str(invoice_id) for invoice_id in sorted(invoice_ids))
    else:
        targets = "auto"
    return stable_hash(f"allocation:{mode}:{amount.quantize(_CENT)}:{targets}")


def _claim_allocation_intent(payment, intent: str) -> None:
    metadata = payment.metadata if isinstance(payment.metadata, dict) else {}
    recorded = metadata.get("_allocation_intent")
    if recorded is not None and recorded != intent:
        raise ConflictException(
            _("This payment was already allocated with another intent."),
            code="allocation_intent_mismatch",
        )
    if recorded is None:
        payment.metadata = {**metadata, "_allocation_intent": intent}
        payment.save(update_fields=["metadata", "updated_at"])


def _assert_existing_oldest_due_retry(
    *,
    existing: list[PaymentAllocation],
    amount: Decimal,
    invoice_ids: set[int] | None,
) -> None:
    existing_total = sum((allocation.amount_uzs for allocation in existing), _ZERO).quantize(_CENT)
    existing_invoice_ids = {allocation.invoice_id for allocation in existing}
    if existing_total != amount or (
        invoice_ids is not None and not existing_invoice_ids.issubset(invoice_ids)
    ):
        raise ConflictException(
            _("This payment was already allocated with another intent."),
            code="allocation_intent_mismatch",
        )


@transaction.atomic
def allocate_payment(
    *, payment_id: int, amount_uzs: Decimal, invoice_ids: list[int] | None = None
) -> list[PaymentAllocation]:
    """Split `amount_uzs` of payment `payment_id` across invoices, oldest-due
    first, with EXACT Decimal accounting (sum of allocations == `amount_uzs`, no
    rounding loss). Over-allocation (more than the targets owe) raises
    `ValidationException`. Idempotent on `payment_id`: re-calling with an already
    allocated payment returns the existing allocations unchanged.

    Lane B calls this from the webhook completion path (import this function
    lazily there)."""
    amount = Decimal(amount_uzs).quantize(_CENT)
    if amount <= _ZERO:
        raise ValidationException(_("Allocation amount must be positive."), code="invalid_amount")

    payment = _locked_attributed_payment(payment_id)
    _assert_payment_completed(payment)
    payment_scope = (payment.branch_at_payment_id, payment.department_at_payment_id)
    if amount > payment.amount_uzs:
        raise ValidationException(
            _("Allocation exceeds the amount received."),
            code="allocation_exceeds_payment",
        )
    if amount < payment.amount_uzs:
        raise ValidationException(
            _("The complete payment amount must be allocated in one immutable operation."),
            code="allocation_total_mismatch",
        )
    requested_invoice_ids = set(invoice_ids) if invoice_ids else None
    intent = _allocation_intent(
        mode="oldest_due",
        amount=amount,
        invoice_ids=requested_invoice_ids,
    )
    existing = list(PaymentAllocation.objects.filter(payment_id=payment_id))
    if existing:  # already allocated — idempotent no-op
        _assert_existing_oldest_due_retry(
            existing=existing,
            amount=amount,
            invoice_ids=requested_invoice_ids,
        )
        _claim_allocation_intent(payment, intent)
        return existing
    _claim_allocation_intent(payment, intent)

    if invoice_ids:
        invoices = list(
            Invoice.objects.select_for_update()
            .filter(pk__in=invoice_ids, status__in=OPEN_STATUSES)
            .order_by("due_date", "id")
        )
        if len(invoices) != len(set(invoice_ids)):
            raise ValidationException(
                _("One or more invoices are not open for allocation."), code="invoice_not_open"
            )
    else:
        invoices = list(
            Invoice.objects.select_for_update()
            .filter(
                status__in=OPEN_STATUSES,
                branch_at_issue_id=payment_scope[0],
                department_at_issue_id=payment_scope[1],
                attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
            )
            .order_by("due_date", "id")
        )

    _assert_allocation_scope(invoices=invoices, payment_scope=payment_scope)

    total_due = sum((_outstanding_for(inv) for inv in invoices), _ZERO)
    if amount > total_due:
        raise ValidationException(
            _("Payment exceeds outstanding balance by %(x)s UZS.") % {"x": amount - total_due},
            code="over_allocation",
            fields={"amount_uzs": [str(amount)], "outstanding_uzs": [str(total_due)]},
        )

    remaining = amount
    allocations: list[PaymentAllocation] = []
    for invoice in invoices:
        if remaining <= _ZERO:
            break
        owed = _outstanding_for(invoice)
        if owed <= _ZERO:
            continue
        take = min(owed, remaining)
        allocations.append(
            PaymentAllocation.objects.create(invoice=invoice, payment_id=payment_id, amount_uzs=take)
        )
        remaining -= take
        _refresh_invoice_status(invoice)

    # Exactness backstop: the loop consumes exactly `amount` because amount <=
    # total_due and each `take` is an exact Decimal min — `remaining` lands on 0.
    if remaining != _ZERO:  # pragma: no cover - defensive
        raise ValidationException(_("Allocation could not be balanced."), code="allocation_unbalanced")
    return allocations


@transaction.atomic
def allocate_payment_lines(*, payment_id: int, lines: list[dict[str, Any]]) -> list[PaymentAllocation]:
    """Allocate a payment across explicit ``(invoice, amount)`` lines — the manual
    allocation endpoint's contract, where the operator chooses exactly how much of the
    payment lands on each invoice (unlike ``allocate_payment``, which splits a single
    total oldest-due-first). Each line must target an OPEN invoice and must not push
    that invoice past its outstanding balance; two lines naming the same invoice sum
    against that one balance. Idempotent on ``payment_id`` — a payment that already has
    allocations returns them unchanged, matching ``allocate_payment``.

    Split out of ``allocate_manual`` (payments): calling ``allocate_payment`` once per
    line silently no-opped every line after the first (its idempotency guard sees the
    first line's rows), dropping money while the payment was marked ALLOCATED."""
    payment = _locked_attributed_payment(payment_id)
    _assert_payment_completed(payment)
    payment_scope = (payment.branch_at_payment_id, payment.department_at_payment_id)
    if not lines:
        raise ValidationException(_("At least one allocation line is required."), code="no_allocations")

    # Aggregate per invoice first so duplicate lines sum against a single outstanding
    # balance, and lock every target row for the balance checks below.
    per_invoice: dict[int, Decimal] = {}
    for line in lines:
        inv_id = int(line["invoice"])
        amount = Decimal(str(line["amount"])).quantize(_CENT)
        if amount <= _ZERO:
            raise ValidationException(_("Allocation amount must be positive."), code="invalid_amount")
        per_invoice[inv_id] = per_invoice.get(inv_id, _ZERO) + amount

    total = sum(per_invoice.values(), _ZERO).quantize(_CENT)
    if total > payment.amount_uzs:
        raise ValidationException(
            _("Allocations exceed the amount received."),
            code="allocation_exceeds_payment",
        )
    if total < payment.amount_uzs:
        raise ValidationException(
            _("The complete payment amount must be allocated in one immutable operation."),
            code="allocation_total_mismatch",
        )
    intent = _allocation_intent(
        mode="explicit_lines",
        amount=total,
        invoice_amounts=per_invoice,
    )
    existing = list(PaymentAllocation.objects.filter(payment_id=payment_id))
    if existing:
        existing_by_invoice: dict[int, Decimal] = {}
        for allocation in existing:
            existing_by_invoice[allocation.invoice_id] = (
                existing_by_invoice.get(allocation.invoice_id, _ZERO) + allocation.amount_uzs
            ).quantize(_CENT)
        if existing_by_invoice != per_invoice:
            raise ConflictException(
                _("This payment was already allocated with another intent."),
                code="allocation_intent_mismatch",
            )
        _claim_allocation_intent(payment, intent)
        return existing
    _claim_allocation_intent(payment, intent)

    invoices = {
        inv.pk: inv
        for inv in Invoice.objects.select_for_update()
        .filter(pk__in=list(per_invoice), status__in=OPEN_STATUSES)
        .order_by("due_date", "id")
    }
    if len(invoices) != len(per_invoice):
        raise ValidationException(
            _("One or more invoices are not open for allocation."), code="invoice_not_open"
        )

    _assert_allocation_scope(invoices=invoices.values(), payment_scope=payment_scope)

    allocations: list[PaymentAllocation] = []
    for inv_id, amount in per_invoice.items():
        invoice = invoices[inv_id]
        owed = _outstanding_for(invoice)
        if amount > owed:
            raise ValidationException(
                _("Allocation to invoice %(n)s exceeds its outstanding balance.") % {"n": invoice.number},
                code="over_allocation",
                fields={"amount_uzs": [str(amount)], "outstanding_uzs": [str(owed)]},
            )
        allocations.append(
            PaymentAllocation.objects.create(invoice=invoice, payment_id=payment_id, amount_uzs=amount)
        )
        _refresh_invoice_status(invoice)
    return allocations


# ---------------------------------------------------------------------------
# Refunds (state machine) — Lane B drives this
# ---------------------------------------------------------------------------

_REFUND_TRANSITIONS: dict[str, set[str]] = {
    Refund.State.REQUESTED: {Refund.State.APPROVED, Refund.State.FAILED},
    Refund.State.APPROVED: {Refund.State.SENT_TO_PROVIDER, Refund.State.FAILED},
    Refund.State.SENT_TO_PROVIDER: {Refund.State.COMPLETED, Refund.State.FAILED},
    Refund.State.COMPLETED: set(),
    Refund.State.FAILED: set(),
}


@transaction.atomic
def request_refund(
    *,
    invoice: Invoice,
    amount_uzs: Decimal,
    reason: str = "",
    payment_id: int | None = None,
    requested_by=None,
    provider: str = "",
) -> Refund:
    amount = Decimal(amount_uzs).quantize(_CENT)
    if amount <= _ZERO:
        raise ValidationException(_("Refund amount must be positive."), code="invalid_amount")
    payment_scope: tuple[int, int | None] | None = None
    if payment_id is not None:
        # Preserve the payment -> invoice lock order used by the payments
        # service, then require both immutable historical snapshots to agree.
        payment_scope = _locked_payment_scope(payment_id)
    # The invoice is the common lock for every refund slice. Without it, two
    # simultaneous requests can both observe the same paid balance and each
    # reserve the full amount before either Refund row becomes visible.
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.branch_at_issue_id is None or invoice.attribution_status not in ATTRIBUTED_SCOPE_STATUSES:
        raise ValidationException(
            _("The invoice's historical ownership must be reviewed before refunding."),
            code="invoice_attribution_unavailable",
        )
    if payment_scope is not None and payment_scope != (
        invoice.branch_at_issue_id,
        invoice.department_at_issue_id,
    ):
        raise ValidationException(
            _("The payment and invoice historical scopes do not match."),
            code="refund_scope_mismatch",
        )
    allocated = invoice.allocations.aggregate(s=Sum("amount_uzs"))["s"] or _ZERO
    # `allocated` is the amount STILL paid: a COMPLETED refund already deleted its
    # allocation rows (reverse_allocations_for_payment), so it is reflected here.
    # We must still subtract any IN-FLIGHT refunds (requested/approved/sent) that
    # have not yet reversed their allocations, or two concurrent refund requests
    # could each pass against the same gross paid amount (over-credit).
    in_flight_states = (
        Refund.State.REQUESTED,
        Refund.State.APPROVED,
        Refund.State.SENT_TO_PROVIDER,
    )
    in_flight = (
        invoice.refunds.filter(state__in=in_flight_states).aggregate(s=Sum("amount_uzs"))["s"] or _ZERO
    )
    net_paid = (allocated - in_flight).quantize(_CENT)
    if amount > net_paid:
        raise ValidationException(
            _("Refund exceeds the amount paid on this invoice."), code="refund_exceeds_paid"
        )
    # Per-(payment, THIS invoice) ceiling: when refunding a specific payment against
    # this invoice, it can never be refunded for more than that payment contributed TO
    # THIS INVOICE. This must be scoped to the invoice to match what register_refund_
    # completion actually reverses — reverse_allocations_for_payment(payment_id,
    # invoice_id) releases only the payment's allocation to THIS invoice. A payment-wide
    # ceiling here would pass a refund larger than the payment's share of this invoice,
    # yet the reversal can release only that share -> money out with no matching restored
    # receivable (ledger shortfall). Filtering by invoice keeps the ceiling == releasable.
    if payment_id is not None:
        pay_allocated = (
            PaymentAllocation.objects.filter(payment_id=payment_id, invoice=invoice).aggregate(
                s=Sum("amount_uzs")
            )["s"]
            or _ZERO
        )
        pay_in_flight = (
            Refund.objects.filter(
                payment_id=payment_id, invoice=invoice, state__in=in_flight_states
            ).aggregate(s=Sum("amount_uzs"))["s"]
            or _ZERO
        )
        pay_refundable = (pay_allocated - pay_in_flight).quantize(_CENT)
        if amount > pay_refundable:
            raise ValidationException(
                _("Refund exceeds the amount this payment contributed to this invoice."),
                code="refund_exceeds_payment",
            )
    return Refund.objects.create(
        invoice=invoice,
        amount_uzs=amount,
        reason=reason,
        payment_id=payment_id,
        requested_by=requested_by,
        provider=provider,
    )


def completed_refund_total_for_payment(payment_id: int) -> Decimal:
    """Sum of COMPLETED refunds tied to a payment — used to decide whether a
    payment has been refunded in full (vs a partial refund)."""
    return (
        Refund.objects.filter(payment_id=payment_id, state=Refund.State.COMPLETED).aggregate(
            s=Sum("amount_uzs")
        )["s"]
        or _ZERO
    )


@transaction.atomic
def transition_refund(*, refund_id: int, to_state: str, actor=None) -> Refund:
    """Move a refund along its state machine; an illegal jump raises
    `ValidationException`."""
    refund = Refund.objects.select_for_update().filter(pk=refund_id).first()
    if refund is None:
        raise NotFoundException(_("Refund not found."), code="refund_not_found")
    if to_state not in _REFUND_TRANSITIONS.get(refund.state, set()):
        raise ValidationException(
            _("Cannot move refund from %(f)s to %(t)s.") % {"f": refund.state, "t": to_state},
            code="invalid_refund_transition",
        )
    if to_state == Refund.State.APPROVED and actor is None:
        raise PermissionException(_("An identified approver is required."), code="approver_required")
    if (
        to_state == Refund.State.APPROVED
        and actor is not None
        and refund.requested_by_id == getattr(actor, "id", None)
    ):
        raise PermissionException(_("You cannot approve your own refund request."), code="self_approval")
    refund.state = to_state
    fields = ["state", "updated_at"]
    if to_state == Refund.State.APPROVED and actor is not None:
        refund.approved_by = actor
        fields.append("approved_by")
    refund.save(update_fields=fields)
    return refund


@transaction.atomic
def register_refund_completion(
    refund_id: int,
    payment_id: int | None = None,
    *,
    provider: str,
    provider_refund_id: str,
) -> Refund:
    """Lane B entry point: mark a refund completed once the provider confirms the
    reversal (e.g. Payme CancelTransaction state -2). Idempotent — a completed
    refund is returned unchanged. Stamps `payment_id` if supplied. Walks the
    state machine forward to `completed` regardless of the current intermediate
    state (approved/sent_to_provider).

    On the first transition to COMPLETED it also reverses the accounting:
    `reverse_allocations_for_payment` deletes the matching `PaymentAllocation`
    rows (scoped to the refunded amount) and re-runs `_refresh_invoice_status`,
    so a fully-refunded invoice flips PAID -> PARTIALLY_PAID/ISSUED and the
    student's outstanding balance returns to its pre-payment value."""
    if (
        not isinstance(provider, str)
        or not provider
        or len(provider) > 16
        or provider.strip() != provider
        or any(ord(character) < 32 or ord(character) == 127 for character in provider)
        or not isinstance(provider_refund_id, str)
        or not provider_refund_id
        or len(provider_refund_id) > 128
        or provider_refund_id.strip() != provider_refund_id
        or any(ord(character) < 32 or ord(character) == 127 for character in provider_refund_id)
    ):
        raise ValidationException(
            _("Provider confirmation is required to complete a refund."),
            code="provider_confirmation_required",
        )
    # Establish the global payment -> refund/invoice lock order. A non-locking
    # pre-read discovers an already-bound payment; after locking the refund we
    # recheck that association and fail on any concurrent/mismatched change.
    stored_payment_id = Refund.objects.filter(pk=refund_id).values_list("payment_id", flat=True).first()
    effective_payment_id = payment_id if payment_id is not None else stored_payment_id
    if effective_payment_id is None:
        raise ValidationException(
            _("A source payment is required to complete a refund."),
            code="refund_payment_required",
        )
    payment = _locked_attributed_payment(effective_payment_id)
    refund = (
        Refund.objects.select_for_update()
        .select_related("invoice", "invoice__student")
        .filter(pk=refund_id)
        .first()
    )
    if refund is None:
        raise NotFoundException(_("Refund not found."), code="refund_not_found")
    if refund.payment_id is not None and refund.payment_id != effective_payment_id:
        raise ValidationException(
            _("Refund confirmation does not match its payment."),
            code="refund_payment_mismatch",
        )
    if payment_id is not None and refund.payment_id is None:
        refund.payment_id = payment_id
    invoice = refund.invoice
    if invoice.branch_at_issue_id is None or invoice.attribution_status not in ATTRIBUTED_SCOPE_STATUSES:
        raise ValidationException(
            _("The invoice's historical ownership must be reviewed before refunding."),
            code="invoice_attribution_unavailable",
        )
    if (
        payment.branch_at_payment_id,
        payment.department_at_payment_id,
    ) != (
        invoice.branch_at_issue_id,
        invoice.department_at_issue_id,
    ):
        raise ValidationException(
            _("The payment and invoice historical scopes do not match."),
            code="refund_scope_mismatch",
        )
    already_completed = (
        Refund.objects.filter(
            payment_id=payment.pk,
            state=Refund.State.COMPLETED,
        )
        .exclude(pk=refund.pk)
        .aggregate(total=Sum("amount_uzs"))["total"]
        or _ZERO
    )
    if already_completed + refund.amount_uzs > payment.amount_uzs:
        raise ValidationException(
            _("Refund exceeds the amount received for this payment."),
            code="refund_exceeds_payment",
        )
    if refund.provider and refund.provider != provider:
        raise ValidationException(
            _("Refund confirmation came from the wrong provider."),
            code="refund_provider_mismatch",
        )
    if (
        Refund.objects.filter(
            provider=provider,
            provider_refund_id=provider_refund_id,
        )
        .exclude(pk=refund.pk)
        .exists()
    ):
        raise ConflictException(
            _("This provider confirmation already belongs to another refund."),
            code="refund_confirmation_reused",
        )
    if refund.state == Refund.State.COMPLETED:
        if refund.provider_refund_id != provider_refund_id:
            raise ValidationException(
                _("Refund is already confirmed under another provider reference."),
                code="refund_confirmation_mismatch",
            )
        return refund
    refund.state = Refund.State.COMPLETED
    refund.provider = provider
    refund.provider_refund_id = provider_refund_id
    refund.provider_confirmed_at = timezone.now()
    if refund.ledger_entry_id is None:
        from apps.approvals.models import LedgerEntry

        refund.ledger_entry = LedgerEntry.objects.create(
            direction=LedgerEntry.Direction.OUT,
            entry_type="refund",
            amount_uzs=refund.amount_uzs,
            branch_id=invoice.branch_at_issue_id,
            party_label=str(invoice.student)[:200],
            source_kind="refund",
            source_id=refund.pk,
            note=refund.reason[:255],
            created_by=refund.requested_by,
        )
    try:
        # The conditional unique constraint is the race-safe backstop for two
        # confirmations that arrive together under the same provider reference.
        with transaction.atomic():
            refund.save(
                update_fields=[
                    "state",
                    "payment_id",
                    "provider",
                    "provider_refund_id",
                    "provider_confirmed_at",
                    "ledger_entry",
                    "updated_at",
                ]
            )
    except IntegrityError as exc:
        raise ConflictException(
            _("This provider confirmation already belongs to another refund."),
            code="refund_confirmation_reused",
        ) from exc
    # Reverse the allocations this refund undoes so the invoice no longer reads
    # as fully paid (the money was returned). Scope to the refund amount so a
    # partial refund only releases that much.
    if refund.payment_id is not None:
        # Scope the reversal to the invoice the Refund NAMES. A single payment can be
        # manually allocated across several invoices; without this scope the reversal
        # released the payment's allocations newest-first across ALL invoices, so a
        # refund attributed to invoice A could silently reopen invoice B (and leave A's
        # recorded balance inconsistent with its Refund). The invoice-level refund
        # ceiling already counts only this invoice's allocations, so releasing only this
        # invoice's allocation of the payment keeps the two consistent.
        reverse_allocations_for_payment(
            payment_id=refund.payment_id, amount_uzs=refund.amount_uzs, invoice_id=refund.invoice_id
        )
        from apps.payments.models import Payment

        payment = Payment.objects.select_for_update().filter(pk=refund.payment_id).first()
        if payment is not None:
            completed = completed_refund_total_for_payment(payment.pk)
            if completed >= payment.amount_uzs and payment.status != Payment.Status.REFUNDED:
                payment.status = Payment.Status.REFUNDED
                payment.save(update_fields=["status", "updated_at"])
    return refund


@transaction.atomic
def reverse_allocations_for_payment(
    *, payment_id: int, amount_uzs: Decimal | None = None, invoice_id: int | None = None
) -> Decimal:
    """Release (up to) `amount_uzs` of a payment's allocations and refresh the
    affected invoices' statuses.

    Deletes whole `PaymentAllocation` rows newest-first; the final row touched by
    a partial reversal is shrunk in place (its CheckConstraint forbids a non-
    positive amount, so a fully-consumed row is deleted instead). `amount_uzs=None`
    reverses the entire payment. `invoice_id` (when set) restricts the reversal to that
    ONE invoice's allocation of the payment (invoice-scoped refund); None reverses
    across all of the payment's invoices. Returns the total UZS actually released. Each
    affected invoice is re-evaluated via `_refresh_invoice_status`, flipping a
    previously PAID invoice back to PARTIALLY_PAID/ISSUED so
    `outstanding_balance` is restored. Idempotent: once the rows are gone a re-call
    releases nothing."""
    qs = PaymentAllocation.objects.select_for_update().filter(payment_id=payment_id)
    if invoice_id is not None:
        qs = qs.filter(invoice_id=invoice_id)
    allocations = list(qs.order_by("-created_at", "-id"))
    if not allocations:
        return _ZERO

    target = (
        Decimal(amount_uzs).quantize(_CENT)
        if amount_uzs is not None
        else sum((a.amount_uzs for a in allocations), _ZERO)
    )
    released = _ZERO
    affected_invoice_ids: set[int] = set()
    for alloc in allocations:
        if released >= target:
            break
        remaining = (target - released).quantize(_CENT)
        affected_invoice_ids.add(alloc.invoice_id)
        if alloc.amount_uzs <= remaining:
            released += alloc.amount_uzs
            alloc.delete()
        else:
            # Partial reversal of this row: shrink it (stays positive).
            alloc.amount_uzs = (alloc.amount_uzs - remaining).quantize(_CENT)
            alloc.save(update_fields=["amount_uzs"])
            released += remaining

    for invoice in Invoice.objects.select_for_update().filter(pk__in=affected_invoice_ids):
        _refresh_invoice_status(invoice)
    return released.quantize(_CENT)


# ---------------------------------------------------------------------------
# Payment plans (D3-A-1, validated sum)
# ---------------------------------------------------------------------------


@transaction.atomic
def create_payment_plan(*, invoice: Invoice, installments: list[dict], created_by=None) -> PaymentPlan:
    """Create an installment plan whose amounts must sum EXACTLY to
    `invoice.total_uzs`. `installments=[{due_date, amount_uzs}]`."""
    if hasattr(invoice, "payment_plan"):
        raise ConflictException(_("This invoice already has a payment plan."), code="plan_exists")
    if not installments:
        raise ValidationException(_("A plan needs at least one installment."), code="empty_plan")
    amounts = [Decimal(str(i["amount_uzs"])).quantize(_CENT) for i in installments]
    if any(amount <= _ZERO for amount in amounts):
        raise ValidationException(
            _("Every installment amount must be positive."),
            code="invalid_installment_amount",
        )
    total = sum(amounts, _ZERO)
    if total != invoice.total_uzs:
        raise ValidationException(
            _("Installments must sum to the invoice total (%(t)s).") % {"t": invoice.total_uzs},
            code="plan_sum_mismatch",
            fields={"sum": [str(total)], "total_uzs": [str(invoice.total_uzs)]},
        )
    plan = PaymentPlan.objects.create(invoice=invoice, created_by=created_by)
    PaymentPlanInstallment.objects.bulk_create(
        PaymentPlanInstallment(
            plan=plan,
            due_date=i["due_date"],
            amount_uzs=Decimal(str(i["amount_uzs"])).quantize(_CENT),
        )
        for i in installments
    )
    return plan


# ---------------------------------------------------------------------------
# Cashier shifts (D3-A-5)
# ---------------------------------------------------------------------------


@transaction.atomic
def open_cashier_shift(
    *, cashier, branch, opening_cash_uzs: Decimal = _ZERO, notes: str = ""
) -> CashierShift:
    """Open a shift. A cashier may only have ONE open shift at a time
    (409-style `ConflictException`)."""
    opening = Decimal(opening_cash_uzs).quantize(_CENT)
    if opening < _ZERO:
        raise ValidationException(_("Opening cash cannot be negative."), code="invalid_amount")
    if CashierShift.objects.filter(cashier=cashier, status=CashierShift.Status.OPEN).exists():
        raise ConflictException(_("This cashier already has an open shift."), code="shift_already_open")
    try:
        with transaction.atomic():
            return CashierShift.objects.create(
                cashier=cashier,
                branch=branch,
                opening_cash_uzs=opening,
                notes=notes,
            )
    except IntegrityError as exc:
        raise ConflictException(
            _("This cashier already has an open shift."), code="shift_already_open"
        ) from exc


@transaction.atomic
def close_cashier_shift(
    *, shift: CashierShift, closing_cash_uzs: Decimal, notes: str = "", actor=None
) -> CashierShift:
    """Close a shift, computing `discrepancy = closing_cash - (opening_cash +
    cash payments in shift)`."""
    shift = CashierShift.objects.select_for_update().get(pk=shift.pk)
    if actor is None:
        raise PermissionException(_("An identified cashier is required."), code="cashier_required")
    if shift.cashier_id != getattr(actor, "id", None) and not getattr(actor, "is_superuser", False):
        raise PermissionException(_("Only the shift cashier may close it."), code="out_of_scope")
    if shift.status == CashierShift.Status.CLOSED:
        raise ConflictException(_("Shift is already closed."), code="shift_closed")
    closing = Decimal(closing_cash_uzs).quantize(_CENT)
    if closing < _ZERO:
        raise ValidationException(_("Closing cash cannot be negative."), code="invalid_amount")
    cash_in = _shift_cash_total(shift)
    shift.closing_cash_uzs = closing
    shift.discrepancy_uzs = (closing - (shift.opening_cash_uzs + cash_in)).quantize(_CENT)
    shift.status = CashierShift.Status.CLOSED
    shift.closed_at = timezone.now()
    shift.closed_by = actor
    if notes:
        shift.notes = notes
    shift.save(
        update_fields=[
            "closing_cash_uzs",
            "discrepancy_uzs",
            "status",
            "closed_at",
            "closed_by",
            "notes",
        ]
    )
    return shift


def _shift_cash_total(shift: CashierShift) -> Decimal:
    """Sum of CASH payments recorded against this shift. Tolerates Lane B not yet
    merged (no Payment model / FK) — returns 0 in that case (D3-A-5 acceptance)."""
    try:
        from apps.payments.models import Payment
    except Exception:  # Lane B not merged yet
        return _ZERO
    total = (
        Payment.objects.filter(
            cashier_shift_id=shift.pk,
            provider="cash",
            status="completed",
            attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
        ).aggregate(s=Sum("amount_uzs"))["s"]
        or _ZERO
    )
    return Decimal(total).quantize(_CENT)


# ---------------------------------------------------------------------------
# Late-payment reminders (D3-A-8 beat body)
# ---------------------------------------------------------------------------


def emit_payment_reminders(*, today: date | None = None) -> int:
    """Scan invoices with ``due_date < today`` that still owe money and
    emit `payment_reminder` once per invoice per `payment_reminder_interval_days`
    cycle. Dedupe via a cache key so re-running the same day sends nothing.
    Returns the count of reminders emitted. Runs in the active tenant schema."""
    from django.core.cache import cache

    today = today or timezone.localdate()
    settings = get_center_settings()
    interval = getattr(settings, "payment_reminder_interval_days", None) or 3
    schema = current_schema()

    # Keep already-overdue rows in the scan. Excluding them after the first status
    # flip meant an invoice could receive its first reminder but never a later
    # interval's reminder.
    overdue = Invoice.objects.filter(
        due_date__lt=today,
        attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
        status__in=(
            Invoice.Status.ISSUED,
            Invoice.Status.PARTIALLY_PAID,
            Invoice.Status.OVERDUE,
        ),
    ).select_related("student")

    emitted = 0
    from apps.finance.signals import payment_reminder

    for invoice in overdue.iterator():
        if invoice.due_date is None:  # overdue filter excludes these; guard for typing/safety
            continue
        # Invoice is an audited model. Use model.save() so the pre/post-save
        # receivers persist the overdue transition; QuerySet.update() would make
        # this compliance-relevant status change invisible in the audit trail.
        if invoice.status != Invoice.Status.OVERDUE:
            invoice.status = Invoice.Status.OVERDUE
            invoice.save(update_fields=["status", "updated_at"])
        # Per-invoice dedupe bucket: floor(days_overdue / interval) so we emit at
        # most once per interval window; cache TTL covers the window.
        days_overdue = (today - invoice.due_date).days
        bucket = days_overdue // interval
        # Carry the exact cycle used by the producer through to notification
        # deduplication.  A receiver-side "today" fallback can collapse or repeat
        # events when a queued signal is retried on another day.
        reminder_cycle = f"{invoice.due_date.isoformat()}:{interval}:{bucket}"
        key = f"finance:reminder:{schema}:{invoice.pk}:{bucket}"
        # ``add`` is atomic on the production Redis backend. A get/set pair lets
        # two Beat invocations both observe a miss and double-dispatch the cycle.
        if not cache.add(key, 1, timeout=interval * 24 * 60 * 60):
            continue
        student_id = invoice.student_id
        payment_reminder.send(
            sender=Invoice,
            invoice_id=invoice.pk,
            student_id=student_id,
            reminder_cycle=reminder_cycle,
            schema_name=schema,
        )
        emitted += 1

    return emitted


# ---------------------------------------------------------------------------
# Statement of account (D3-A-7: weasyprint -> S3 -> signed URL, off-request)
# ---------------------------------------------------------------------------


def render_statement_pdf(
    *,
    student,
    locale: str = "en",
    invoice_ids: list[int] | None = None,
) -> bytes:
    """Render the statement HTML to PDF bytes. weasyprint is imported LAZILY so
    the app loads where its GTK native libs are absent (Windows dev box)."""
    from django.template.loader import render_to_string
    from django.utils import translation
    from weasyprint import HTML  # lazy: GTK native libs only needed here

    from apps.finance.selectors import statement_context

    context = statement_context(student=student, invoice_ids=invoice_ids)
    template = f"documents/statement_{locale}.html"
    with translation.override(locale):
        html = render_to_string(template, context)
    return HTML(string=html).write_pdf()


@dataclass(frozen=True)
class GeneratedStatement:
    """Server-derived statement artifact and the immutable rows it contains."""

    key: str
    invoice_ids: tuple[int, ...]


def generate_statement_artifact(
    student_id: int,
    *,
    locale: str = "en",
    requested_by_id: int | None = None,
) -> GeneratedStatement:
    """Render one statement and bind it to its exact authorized invoice set."""
    student = StudentProfile.objects.select_related("user").get(pk=student_id)
    invoice_ids: list[int] | None = None
    if requested_by_id is not None:
        from apps.finance.selectors import scoped_invoices
        from apps.users.models import User
        from core.permissions import get_unambiguous_user_roles, has_permission_code

        requested_by = User.objects.filter(pk=requested_by_id, is_active=True).first()
        if requested_by is None:
            raise PermissionException(_("Statement access is no longer available."), code="forbidden")
        roles = get_unambiguous_user_roles(requested_by)
        if not (requested_by.is_superuser or has_permission_code(roles, "finance:read")):
            raise PermissionException(_("Statement access is no longer available."), code="forbidden")
        invoice_ids = list(
            scoped_invoices(user=requested_by, roles=roles)
            .filter(student_id=student_id)
            .values_list("pk", flat=True)
        )
        if not invoice_ids:
            raise NotFoundException(_("Statement records were not found."), code="not_found")
        if len(invoice_ids) > 5000:
            raise UnprocessableEntity(
                _("The statement contains too many records."),
                code="statement_too_large",
            )
    if invoice_ids is None:
        pdf = render_statement_pdf(student=student, locale=locale)
    else:
        pdf = render_statement_pdf(
            student=student,
            locale=locale,
            invoice_ids=invoice_ids,
        )
    # A random artifact suffix prevents two authorized users who request a
    # transferred student's differently scoped statements in the same second
    # from overwriting one another's object.
    ts = timezone.now().strftime("%Y%m%d%H%M%S")
    key = f"{current_schema()}/documents/statement_{student_id}_{ts}_{uuid4().hex}.pdf"
    from infrastructure.storage.s3_client import upload_bytes

    upload_bytes(key, pdf, content_type="application/pdf")
    return GeneratedStatement(
        key=key,
        invoice_ids=tuple(sorted(invoice_ids or ())),
    )


def generate_statement(
    student_id: int,
    *,
    locale: str = "en",
    requested_by_id: int | None = None,
) -> str:
    """Compatibility wrapper returning only the newly uploaded storage key."""
    return generate_statement_artifact(
        student_id,
        locale=locale,
        requested_by_id=requested_by_id,
    ).key


# ---------------------------------------------------------------------------
# Expenses (F14-1): created -> approved -> paid (or rejected)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_expense(
    *, branch, description: str, amount_uzs: Decimal, category: str = "", created_by=None
) -> Expense:
    expense = Expense.objects.create(
        branch=branch,
        description=description,
        amount_uzs=amount_uzs,
        category=category,
        created_by=created_by,
    )
    from apps.approvals.services import KIND_EXPENSE, create_request

    approval = create_request(
        kind=KIND_EXPENSE,
        title=description[:200],
        description=description,
        amount_uzs=expense.amount_uzs,
        branch=branch,
        requested_by=created_by,
        payload={
            "expense_id": expense.pk,
            "category": category,
            "party_label": description[:200],
        },
    )
    expense.approval_request = approval
    expense.save(update_fields=["approval_request"])
    return expense


def _locked_expense(expense_id: int) -> Expense:
    expense = Expense.objects.select_for_update().filter(pk=expense_id).first()
    if expense is None:
        raise NotFoundException(_("Expense not found."), code="expense_not_found")
    return expense


@transaction.atomic
def approve_expense(*, expense_id: int, actor=None) -> Expense:
    expense = Expense.objects.filter(pk=expense_id).only("id", "approval_request_id").first()
    if expense is None:
        raise NotFoundException(_("Expense not found."), code="expense_not_found")
    if expense.approval_request_id is None:
        raise UnprocessableEntity(_("Expense has no approval request."), code="expense_approval_missing")
    from apps.approvals.services import approve

    approve(request_id=expense.approval_request_id, actor=actor)
    expense.refresh_from_db()
    return expense


@transaction.atomic
def reject_expense(*, expense_id: int, reason: str = "", actor=None) -> Expense:
    expense = Expense.objects.filter(pk=expense_id).only("id", "approval_request_id").first()
    if expense is None:
        raise NotFoundException(_("Expense not found."), code="expense_not_found")
    if expense.approval_request_id is None:
        raise UnprocessableEntity(_("Expense has no approval request."), code="expense_approval_missing")
    from apps.approvals.services import reject

    reject(request_id=expense.approval_request_id, actor=actor, note=reason)
    expense.refresh_from_db()
    return expense


@transaction.atomic
def pay_expense(*, expense_id: int, payment_method_id: int, actor=None) -> Expense:
    """Disburse via the approval engine, which atomically appends the ledger row."""
    expense = Expense.objects.filter(pk=expense_id).only("id", "approval_request_id").first()
    if expense is None:
        raise NotFoundException(_("Expense not found."), code="expense_not_found")
    if expense.approval_request_id is None:
        raise UnprocessableEntity(_("Expense has no approval request."), code="expense_approval_missing")
    from apps.approvals.services import disburse

    disburse(
        request_id=expense.approval_request_id,
        payment_method_id=payment_method_id,
        actor=actor,
        direction="out",
        entry_type="expense",
        party_label="",
    )
    expense.refresh_from_db()
    return expense
