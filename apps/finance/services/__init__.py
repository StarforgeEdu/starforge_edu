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

from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.finance.models import (
    CashierShift,
    Discount,
    Expense,
    FeeSchedule,
    Invoice,
    InvoiceLine,
    PaymentAllocation,
    PaymentMethod,
    PaymentPlan,
    PaymentPlanInstallment,
    Refund,
)
from apps.finance.signals import invoice_issued
from apps.org.selectors import get_center_settings
from apps.students.models import StudentProfile
from core.exceptions import ConflictException, NotFoundException, UnprocessableEntity, ValidationException
from core.utils import current_schema, stable_hash

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")

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
    manual = getattr(settings, "fx_rate_usd_manual", None)
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
    return Decimal(str(raw)) if raw is not None else None


def _usd_total(total_uzs: Decimal, rate: Decimal | None) -> Decimal | None:
    if rate is None or rate == _ZERO:
        return None
    return (total_uzs / rate).quantize(_CENT)


# ---------------------------------------------------------------------------
# Discounts (sibling materialization)
# ---------------------------------------------------------------------------


def _has_enrolled_sibling(student: StudentProfile) -> bool:
    """True when `student` shares an active `Guardian` with another ENROLLED/ACTIVE
    student. Routed via parents.Guardian (the sanctioned parent<->student link)."""
    parent_ids = list(student.guardians.values_list("parent_id", flat=True))
    if not parent_ids:
        return False
    return (
        StudentProfile.objects.filter(
            guardians__parent_id__in=parent_ids,
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
    student = StudentProfile.objects.select_related("user").filter(pk=student_id).first()
    if student is None:
        raise NotFoundException(_("Student not found."), code="student_not_found")

    settings = get_center_settings()
    fee_schedule = None
    if fee_schedule_id is not None:
        fee_schedule = FeeSchedule.objects.filter(pk=fee_schedule_id).first()
        if fee_schedule is None:
            raise NotFoundException(_("Fee schedule not found."), code="fee_schedule_not_found")

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
    for raw in lines or []:
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
        {**spec, "amount_uzs": (spec["quantity"] * spec["unit_price_uzs"]).quantize(_CENT)}
        for spec in line_specs
    ]
    gross = sum((line["amount_uzs"] for line in charge_lines), _ZERO)

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
        cohort=student.current_cohort,
        fee_schedule=fee_schedule,
        period=period,
        status=Invoice.Status.ISSUED,
        issue_date=today,
        due_date=_due_date(today, fee_schedule, settings),
        currency=settings.currency_primary or "UZS",
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
        if discount.single_use and not Discount.objects.filter(
            pk=discount.pk, is_active=True
        ).update(is_active=False):
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
    was_overdue = invoice.status == Invoice.Status.OVERDUE
    invoice.due_date = new_due_date
    invoice.save(update_fields=["due_date", "updated_at"])
    # An extension whose new deadline is today-or-later rescues an overdue bill;
    # recompute its status from payments via the single source of truth
    # (_refresh_invoice_status -> issued / partially_paid / paid).
    if was_overdue and new_due_date >= timezone.localdate():
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
    _refresh_invoice_status(invoice)  # issued / partially_paid / paid from allocations
    if invoice.status == Invoice.Status.ISSUED and due_date is not None and due_date < timezone.localdate():
        invoice.status = Invoice.Status.OVERDUE
        invoice.save(update_fields=["status", "updated_at"])
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
    student = StudentProfile.objects.filter(pk=student_id).first()
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
    """Flip issued -> partially_paid -> paid based on allocation total. Never
    downgrades a void invoice."""
    if invoice.status == Invoice.Status.VOID:
        return
    allocated = invoice.allocations.aggregate(s=Sum("amount_uzs"))["s"] or _ZERO
    if allocated >= invoice.total_uzs and invoice.total_uzs > _ZERO:
        new_status = Invoice.Status.PAID
    elif allocated > _ZERO:
        new_status = Invoice.Status.PARTIALLY_PAID
    else:
        new_status = Invoice.Status.ISSUED
    if invoice.status != new_status:
        invoice.status = new_status
        invoice.save(update_fields=["status", "updated_at"])


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

    existing = list(PaymentAllocation.objects.filter(payment_id=payment_id))
    if existing:  # already allocated — idempotent no-op
        return existing

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
            Invoice.objects.select_for_update().filter(status__in=OPEN_STATUSES).order_by("due_date", "id")
        )

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
def allocate_payment_lines(
    *, payment_id: int, lines: list[dict[str, Any]]
) -> list[PaymentAllocation]:
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
    existing = list(PaymentAllocation.objects.filter(payment_id=payment_id))
    if existing:  # already allocated — idempotent no-op
        return existing
    if not lines:
        raise ValidationException(
            _("At least one allocation line is required."), code="no_allocations"
        )

    # Aggregate per invoice first so duplicate lines sum against a single outstanding
    # balance, and lock every target row for the balance checks below.
    per_invoice: dict[int, Decimal] = {}
    for line in lines:
        inv_id = int(line["invoice"])
        amount = Decimal(str(line["amount"])).quantize(_CENT)
        if amount <= _ZERO:
            raise ValidationException(
                _("Allocation amount must be positive."), code="invalid_amount"
            )
        per_invoice[inv_id] = per_invoice.get(inv_id, _ZERO) + amount

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

    allocations: list[PaymentAllocation] = []
    for inv_id, amount in per_invoice.items():
        invoice = invoices[inv_id]
        owed = _outstanding_for(invoice)
        if amount > owed:
            raise ValidationException(
                _("Allocation to invoice %(n)s exceeds its outstanding balance.")
                % {"n": invoice.number},
                code="over_allocation",
                fields={"amount_uzs": [str(amount)], "outstanding_uzs": [str(owed)]},
            )
        allocations.append(
            PaymentAllocation.objects.create(
                invoice=invoice, payment_id=payment_id, amount_uzs=amount
            )
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
) -> Refund:
    amount = Decimal(amount_uzs).quantize(_CENT)
    if amount <= _ZERO:
        raise ValidationException(_("Refund amount must be positive."), code="invalid_amount")
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
    refund.state = to_state
    fields = ["state", "updated_at"]
    if to_state == Refund.State.APPROVED and actor is not None:
        refund.approved_by = actor
        fields.append("approved_by")
    refund.save(update_fields=fields)
    return refund


@transaction.atomic
def register_refund_completion(refund_id: int, payment_id: int | None = None) -> Refund:
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
    refund = Refund.objects.select_for_update().filter(pk=refund_id).first()
    if refund is None:
        raise NotFoundException(_("Refund not found."), code="refund_not_found")
    if payment_id is not None and refund.payment_id is None:
        refund.payment_id = payment_id
    if refund.state == Refund.State.COMPLETED:
        if payment_id is not None:
            refund.save(update_fields=["payment_id", "updated_at"])
        return refund
    refund.state = Refund.State.COMPLETED
    refund.save(update_fields=["state", "payment_id", "updated_at"])
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
    total = sum((Decimal(str(i["amount_uzs"])).quantize(_CENT) for i in installments), _ZERO)
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
    if CashierShift.objects.filter(cashier=cashier, status=CashierShift.Status.OPEN).exists():
        raise ConflictException(_("This cashier already has an open shift."), code="shift_already_open")
    return CashierShift.objects.create(
        cashier=cashier,
        branch=branch,
        opening_cash_uzs=Decimal(opening_cash_uzs).quantize(_CENT),
        notes=notes,
    )


@transaction.atomic
def close_cashier_shift(*, shift: CashierShift, closing_cash_uzs: Decimal, notes: str = "") -> CashierShift:
    """Close a shift, computing `discrepancy = closing_cash - (opening_cash +
    cash payments in shift)`."""
    if shift.status == CashierShift.Status.CLOSED:
        raise ConflictException(_("Shift is already closed."), code="shift_closed")
    closing = Decimal(closing_cash_uzs).quantize(_CENT)
    cash_in = _shift_cash_total(shift)
    shift.closing_cash_uzs = closing
    shift.discrepancy_uzs = (closing - (shift.opening_cash_uzs + cash_in)).quantize(_CENT)
    shift.status = CashierShift.Status.CLOSED
    shift.closed_at = timezone.now()
    if notes:
        shift.notes = notes
    shift.save(update_fields=["closing_cash_uzs", "discrepancy_uzs", "status", "closed_at", "notes"])
    return shift


def _shift_cash_total(shift: CashierShift) -> Decimal:
    """Sum of CASH payments recorded against this shift. Tolerates Lane B not yet
    merged (no Payment model / FK) — returns 0 in that case (D3-A-5 acceptance)."""
    try:
        from apps.payments.models import Payment
    except Exception:  # Lane B not merged yet
        return _ZERO
    total = (
        Payment.objects.filter(cashier_shift_id=shift.pk, provider="cash", status="completed").aggregate(
            s=Sum("amount_uzs")
        )["s"]
        or _ZERO
    )
    return Decimal(total).quantize(_CENT)


# ---------------------------------------------------------------------------
# Late-payment reminders (D3-A-8 beat body)
# ---------------------------------------------------------------------------


def emit_payment_reminders(*, today: date | None = None) -> int:
    """Scan invoices with `due_date < today` in status issued/partially_paid and
    emit `payment_reminder` once per invoice per `payment_reminder_interval_days`
    cycle. Dedupe via a cache key so re-running the same day sends nothing.
    Returns the count of reminders emitted. Runs in the active tenant schema."""
    from django.core.cache import cache

    today = today or timezone.localdate()
    settings = get_center_settings()
    interval = getattr(settings, "payment_reminder_interval_days", None) or 3
    schema = current_schema()

    # Also flip past-due open invoices to `overdue` (status hygiene).
    overdue = Invoice.objects.filter(
        due_date__lt=today,
        status__in=(Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID),
    ).select_related("student")

    emitted = 0
    from apps.finance.signals import payment_reminder

    for invoice in overdue.iterator():
        if invoice.due_date is None:  # overdue filter excludes these; guard for typing/safety
            continue
        # Per-invoice dedupe bucket: floor(days_overdue / interval) so we emit at
        # most once per interval window; cache TTL covers the window.
        days_overdue = (today - invoice.due_date).days
        bucket = days_overdue // interval
        key = f"finance:reminder:{schema}:{invoice.pk}:{bucket}"
        if cache.get(key) is not None:
            continue
        cache.set(key, 1, timeout=interval * 24 * 60 * 60)
        student_id = invoice.student_id
        payment_reminder.send(
            sender=Invoice,
            invoice_id=invoice.pk,
            student_id=student_id,
            schema_name=schema,
        )
        emitted += 1

    # Mark them overdue (separate UPDATE, after the scan, so iteration is stable).
    # PARTIALLY_PAID past-due bills are included: a partial payment must not park a
    # delinquent invoice out of the OVERDUE state (R2-P2). PAID is excluded (settled).
    Invoice.objects.filter(
        due_date__lt=today,
        status__in=(Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID),
    ).update(status=Invoice.Status.OVERDUE)
    return emitted


# ---------------------------------------------------------------------------
# Statement of account (D3-A-7: weasyprint -> S3 -> signed URL, off-request)
# ---------------------------------------------------------------------------


def render_statement_pdf(*, student, locale: str = "en") -> bytes:
    """Render the statement HTML to PDF bytes. weasyprint is imported LAZILY so
    the app loads where its GTK native libs are absent (Windows dev box)."""
    from django.template.loader import render_to_string
    from django.utils import translation
    from weasyprint import HTML  # lazy: GTK native libs only needed here

    from apps.finance.selectors import statement_context

    context = statement_context(student=student)
    template = f"documents/statement_{locale}.html"
    with translation.override(locale):
        html = render_to_string(template, context)
    return HTML(string=html).write_pdf()


def generate_statement(student_id: int, *, locale: str = "en") -> str:
    """Idempotent-ish task body: render the statement and upload it to
    `{schema}/documents/statement_{student_id}_{ts}.pdf`. Returns the S3 key.
    Cache the key under the task id so the result endpoint can sign it."""
    student = StudentProfile.objects.select_related("user").get(pk=student_id)
    pdf = render_statement_pdf(student=student, locale=locale)
    ts = timezone.now().strftime("%Y%m%d%H%M%S")
    key = f"{current_schema()}/documents/statement_{student_id}_{ts}.pdf"
    from infrastructure.storage.s3_client import upload_bytes

    upload_bytes(key, pdf, content_type="application/pdf")
    return key


# ---------------------------------------------------------------------------
# Expenses (F14-1): created -> approved -> paid (or rejected)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_expense(
    *, branch, description: str, amount_uzs: Decimal, category: str = "", created_by=None
) -> Expense:
    return Expense.objects.create(
        branch=branch,
        description=description,
        amount_uzs=amount_uzs,
        category=category,
        created_by=created_by,
    )


def _locked_expense(expense_id: int) -> Expense:
    expense = Expense.objects.select_for_update().filter(pk=expense_id).first()
    if expense is None:
        raise NotFoundException(_("Expense not found."), code="expense_not_found")
    return expense


@transaction.atomic
def approve_expense(*, expense_id: int, actor=None) -> Expense:
    expense = _locked_expense(expense_id)
    if expense.status != Expense.Status.PENDING:
        raise UnprocessableEntity(_("Only a pending expense can be approved."), code="expense_not_pending")
    expense.status = Expense.Status.APPROVED
    expense.approved_by = actor
    expense.approved_at = timezone.now()
    expense.save(update_fields=["status", "approved_by", "approved_at"])
    return expense


@transaction.atomic
def reject_expense(*, expense_id: int, reason: str = "", actor=None) -> Expense:
    expense = _locked_expense(expense_id)
    if expense.status not in (Expense.Status.PENDING, Expense.Status.APPROVED):
        raise UnprocessableEntity(_("This expense can no longer be rejected."), code="expense_not_rejectable")
    expense.status = Expense.Status.REJECTED
    expense.reject_reason = reason
    expense.approved_by = actor
    expense.save(update_fields=["status", "reject_reason", "approved_by"])
    return expense


@transaction.atomic
def pay_expense(*, expense_id: int, payment_method_id: int, actor=None) -> Expense:
    """Disburse an APPROVED expense via a chosen (active) PaymentMethod."""
    expense = _locked_expense(expense_id)
    if expense.status != Expense.Status.APPROVED:
        raise UnprocessableEntity(_("Only an approved expense can be paid."), code="expense_not_approved")
    method = PaymentMethod.objects.filter(pk=payment_method_id, is_active=True).first()
    if method is None:
        raise UnprocessableEntity(_("Unknown or inactive payment method."), code="payment_method_invalid")
    expense.status = Expense.Status.PAID
    expense.payment_method = method
    expense.paid_by = actor
    expense.paid_at = timezone.now()
    expense.save(update_fields=["status", "payment_method", "paid_by", "paid_at"])
    return expense
