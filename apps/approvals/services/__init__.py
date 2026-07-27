"""Approvals + Ledger engine services.

State machine: PENDING -> APPROVED | REJECTED | CANCELLED; APPROVED -> DISBURSED
(money-moving kinds) writes an immutable LedgerEntry. Every transition is locked
with select_for_update so concurrent approve/disburse can't double-act.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.approvals.models import ApprovalRequest, LedgerEntry
from core.exceptions import (
    NotFoundException,
    PermissionException,
    UnprocessableEntity,
    ValidationException,
)
from core.permissions import roles_with_permission

# Kinds whose payload is validated at creation time and which carry an
# on-approval side-effect (see _apply_approval_effect).
KIND_DISCOUNT = "discount"
KIND_PAYMENT_DELAY = "payment_delay"
# A money-moving kind (acts at disburse, not approve) that additionally needs a
# validated borrower in its payload — see _validate_loan_payload (F21-1).
KIND_LOAN = "loan"
# A decision-only KIND (acts at approve, like discount): it issues a charge the
# student OWES (a penalty invoice), never a cash payout — so its amount lives in
# the payload and the request's amount_uzs stays null (it can't be disbursed).
KIND_FINE = "fine"
# A decision-only KIND (acts at approve, like discount): it credits a student for a
# lesson they MISSED — materializes a standing Discount (a negative invoice line),
# gated by a per-center policy and tied to a real absence record (anti-fraud: you can
# only deduct for an absence that actually happened, and only once).
KIND_ABSENCE_DEDUCTION = "absence_deduction"
# A money-OUT kind (acts at disburse) paying a named STAFF recipient (cash reward,
# F17-1). Built by apps.rewards; the recipient's User id is pinned in the payload.
KIND_REWARD = "reward"

# F13-1: a teacher salary payout, its amount COMPUTED from the teacher's dynamic PayoutPolicy
# (apps.teachers) — hourly / %-of-collected-tuition / flat-monthly. Money-OUT to the teacher;
# the teacher's User id + payee label + the computed breakdown are pinned in the payload.
KIND_SALARY_PREP = "salary_prep"

# Money/percent columns are NUMERIC(_, 2); normalize payload values to that scale.
_TWO_PLACES = Decimal("0.01")


def _notify(*, event_type: str, recipient_id: int | None, req: ApprovalRequest) -> None:
    """Best-effort in-app notification on an approval transition (never breaks the
    money transition — failures are swallowed/logged inside dispatch)."""
    if recipient_id is None:
        return
    from apps.notifications.services import dispatch

    dispatch(
        event_type=event_type,
        recipient_id=recipient_id,
        context={
            "kind": req.kind,
            "title": req.title,
            "amount_uzs": str(req.amount_uzs) if req.amount_uzs is not None else "",
            "request_id": req.pk,
        },
    )


def _disburser_ids(req: ApprovalRequest) -> list[int]:
    """Active users who may disburse — scoped to the request's branch when set."""
    from apps.users.models import RoleMembership

    qs = RoleMembership.objects.filter(
        role__in=roles_with_permission("approvals:disburse"), revoked_at__isnull=True
    )
    if req.branch_id:
        qs = qs.filter(branch_id=req.branch_id)
    return list(qs.values_list("user_id", flat=True).distinct())


def _validate_discount_payload(payload: dict) -> dict:
    """Validate + normalize a discount-request payload at creation time, so a
    malformed discount never enters the approval queue (a clean 400, not a 500
    when someone later approves it). Shape:

        {student_id, discount_type?, (percent | fixed_amount_uzs), valid_from?, valid_until?}

    Exactly one of percent / fixed_amount_uzs must be set (mirrors the Discount
    model's XOR CheckConstraint). Numbers are stored as strings to keep the JSON
    payload exact (no float drift) and dates as ISO strings.
    """
    from apps.finance.models import Discount
    from apps.students.models import StudentProfile

    student_id = payload.get("student_id")
    if not isinstance(student_id, int) or not StudentProfile.objects.filter(pk=student_id).exists():
        raise ValidationException(
            _("A discount request needs a valid student_id in its payload."),
            code="discount_student_required",
            fields={"payload": ["student_id"]},
        )

    percent = payload.get("percent")
    fixed = payload.get("fixed_amount_uzs")
    if (percent is None) == (fixed is None):
        raise ValidationException(
            _("Set exactly one of payload.percent or payload.fixed_amount_uzs."),
            code="discount_amount_xor",
        )

    dtype = payload.get("discount_type", Discount.DiscountType.MANUAL)
    if dtype not in Discount.DiscountType.values:
        raise ValidationException(_("Unknown discount_type."), code="discount_type_invalid")

    clean: dict = {"student_id": student_id, "discount_type": dtype}
    if percent is not None:
        try:
            pv = Decimal(str(percent))
        except (InvalidOperation, ValueError):
            raise ValidationException(
                _("percent must be a number."), code="discount_percent_invalid"
            ) from None
        # NaN/Infinity construct fine but are unordered: the range comparison below
        # would raise InvalidOperation (a 500). Exclude them first (payload-reachable).
        if not pv.is_finite():
            raise ValidationException(_("percent must be a finite number."), code="discount_percent_invalid")
        if not (Decimal("0") < pv <= Decimal("100")):
            raise ValidationException(_("percent must be between 0 and 100."), code="discount_percent_range")
        # Quantize to the Discount column's scale (NUMERIC(5,2)) at the gate, so the
        # audited payload always equals the discount that actually bills the student
        # (Postgres would otherwise silently round on insert -> audit divergence).
        clean["percent"] = str(pv.quantize(_TWO_PLACES))
    else:
        try:
            fv = Decimal(str(fixed))
        except (InvalidOperation, ValueError):
            raise ValidationException(
                _("fixed_amount_uzs must be a number."), code="discount_fixed_invalid"
            ) from None
        # NaN/Infinity are unordered: a comparison would raise InvalidOperation (500).
        if not fv.is_finite():
            raise ValidationException(
                _("fixed_amount_uzs must be a finite number."), code="discount_fixed_invalid"
            )
        if fv <= 0:
            raise ValidationException(_("fixed_amount_uzs must be positive."), code="discount_fixed_range")
        # NUMERIC(18,2): at most 16 integer digits. Reject the overflow at the gate
        # as a clean 400 rather than letting it surface as a DB 500 at approve time.
        # The pre-quantize check keeps quantize itself safe (value now < 1e16); the
        # post-quantize re-check catches a value that ROUNDS UP across the boundary.
        if fv >= Decimal("1e16"):
            raise ValidationException(_("fixed_amount_uzs is too large."), code="discount_fixed_range")
        fv = fv.quantize(_TWO_PLACES)
        if fv >= Decimal("1e16"):
            raise ValidationException(_("fixed_amount_uzs is too large."), code="discount_fixed_range")
        clean["fixed_amount_uzs"] = str(fv)

    for key in ("valid_from", "valid_until"):
        raw = payload.get(key)
        if raw:
            try:
                clean[key] = date.fromisoformat(str(raw)).isoformat()
            except ValueError:
                raise ValidationException(
                    _("%(key)s must be an ISO date (YYYY-MM-DD).") % {"key": key},
                    code="discount_date_invalid",
                ) from None
    return clean


def _validate_payment_delay_payload(payload: dict) -> dict:
    """Validate + normalize a payment-delay payload at creation time. Shape:

        {invoice_id, new_due_date}

    The target must be an OPEN invoice with a due date, and new_due_date must be
    strictly later than the current one (you can only delay, never advance/backdate).
    Re-checked again at approve time, since the invoice may move in between.
    """
    from apps.finance.models import Invoice
    from apps.finance.services import OPEN_STATUSES

    invoice_id = payload.get("invoice_id")
    invoice = Invoice.objects.filter(pk=invoice_id).first() if isinstance(invoice_id, int) else None
    if invoice is None:
        raise ValidationException(
            _("A payment-delay request needs a valid invoice_id in its payload."),
            code="payment_delay_invoice_required",
            fields={"payload": ["invoice_id"]},
        )
    if invoice.status not in OPEN_STATUSES:
        raise ValidationException(
            _("Only an open invoice's payment can be delayed."), code="payment_delay_invoice_not_open"
        )
    if invoice.due_date is None:
        raise ValidationException(
            _("This invoice has no due date to extend."), code="payment_delay_no_due_date"
        )

    try:
        new_due = date.fromisoformat(str(payload.get("new_due_date")))
    except ValueError:
        raise ValidationException(
            _("new_due_date must be an ISO date (YYYY-MM-DD)."), code="payment_delay_date_invalid"
        ) from None
    if new_due <= invoice.due_date:
        raise ValidationException(
            _("A payment delay can only move the due date later."), code="payment_delay_not_later"
        )
    if new_due < timezone.localdate():
        # A delay into the past is meaningless: it would leave the bill overdue with
        # no observable grace. Require it to land today or later.
        raise ValidationException(
            _("A payment delay must move the due date to today or later."),
            code="payment_delay_in_past",
        )
    return {"invoice_id": invoice_id, "new_due_date": new_due.isoformat()}


def _validate_salary_prep_payload(payload: dict) -> dict:
    """Validate a salary_prep payload (F13-1). It must name the beneficiary TeacherProfile
    (so SoD extends to them) and carry a `party_label` so the disbursement's immutable
    ledger row names the teacher. The computed `breakdown` + period keys are preserved for
    the audit trail. Built by apps.teachers.services.prepare_salary (which computes the
    amount from the teacher's dynamic PayoutPolicy) — never a raw human-entered figure."""
    teacher_profile_id = payload.get("teacher_profile_id")
    if not isinstance(teacher_profile_id, int) or isinstance(teacher_profile_id, bool):
        raise ValidationException(
            _("A salary request must name the teacher (teacher_profile_id)."),
            code="salary_teacher_required",
            fields={"teacher_profile_id": ["An integer teacher profile id is required."]},
        )
    out = dict(payload)
    if not out.get("party_label"):
        out["party_label"] = "salary"
    out["party_label"] = str(out["party_label"])[:200]
    return out


def _validate_loan_payload(payload: dict) -> dict:
    """Validate a staff-loan payload at creation time. Shape: {borrower_id}.

    The borrower must be an active STAFF member (never a student/parent — a "staff
    loan" pays staff, mirroring the F17-1 rewards recipient guard). Their display
    name is stamped into the payload as `party_label` (truncated to the ledger
    column width), so both the disbursement (money OUT) and every repayment (money
    IN) name the BORROWER on the ledger — not whoever keyed the request — which is
    the "who actually owes the centre" audit line.
    """
    from apps.users.models import User
    from core.permissions import Role

    staff_roles = tuple(r for r in Role.ALL if r not in (Role.STUDENT, Role.PARENT))
    borrower_id = payload.get("borrower_id")
    borrower = (
        User.objects.filter(
            pk=borrower_id,
            is_active=True,
            # Positive role condition on the join → only users WITH a live staff
            # membership match (avoids the LEFT-JOIN isnull trap matching everyone).
            role_memberships__revoked_at__isnull=True,
            role_memberships__role__in=staff_roles,
        )
        .distinct()
        .first()
        if isinstance(borrower_id, int)
        else None
    )
    if borrower is None:
        raise ValidationException(
            _("A loan request needs a valid staff borrower_id in its payload."),
            code="loan_borrower_required",
            fields={"payload": ["borrower_id"]},
        )
    return {"borrower_id": borrower_id, "party_label": (borrower.get_full_name() or borrower.username)[:200]}


def _validate_fine_payload(payload: dict) -> dict:
    """Validate + normalize a fine-request payload at creation time, so a malformed
    fine never enters the queue (a clean 400, not a 500 at approve time). Shape:

        {student_id, amount_uzs, reason?}

    The amount the client sends on the request's top-level `amount_uzs` is folded
    into the payload here (the request row keeps amount_uzs=null so the fine can
    never be paid OUT through disburse — its effect is a charge the student owes).
    The amount is stored as a string at the NUMERIC(18,2) scale so the audited
    payload always equals the penalty line that actually bills the student.
    """
    from apps.students.models import StudentProfile

    student_id = payload.get("student_id")
    # bool is a subclass of int — exclude it so student_id=true can't resolve to pk=1.
    if (
        not isinstance(student_id, int)
        or isinstance(student_id, bool)
        or not StudentProfile.objects.filter(pk=student_id).exists()
    ):
        raise ValidationException(
            _("A fine request needs a valid student_id in its payload."),
            code="fine_student_required",
            fields={"payload": ["student_id"]},
        )

    raw_amount = payload.get("amount_uzs")
    if raw_amount is None:
        raise ValidationException(_("A fine request needs an amount_uzs."), code="fine_amount_required")
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, ValueError):
        raise ValidationException(_("amount_uzs must be a number."), code="fine_amount_invalid") from None
    # NaN/Infinity construct fine but are unordered: a later `<`/`>` would raise
    # InvalidOperation (a 500). Exclude them before any comparison.
    if not amount.is_finite():
        raise ValidationException(_("amount_uzs must be a finite number."), code="fine_amount_invalid")
    # Bound the RAW magnitude to (0, 1e16) before quantize, so quantize can't itself
    # overflow the Decimal context on a huge value; then re-check, since rounding to
    # NUMERIC(18,2) (16 integer digits) can tip 0.00x -> 0 or 9999999999999999.99x ->
    # 1e16. Reject the overflow at the gate as a clean 400, never a DB 500 at issue.
    if not (Decimal("0") < amount < Decimal("1e16")):
        raise ValidationException(_("amount_uzs is out of range."), code="fine_amount_range")
    amount = amount.quantize(_TWO_PLACES)
    if not (Decimal("0") < amount < Decimal("1e16")):
        raise ValidationException(_("amount_uzs is out of range."), code="fine_amount_range")

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        raise ValidationException(_("reason must be text."), code="fine_reason_invalid")
    clean: dict = {"student_id": student_id, "amount_uzs": str(amount), "reason": reason[:255]}

    # F24-1: a fine MAY cite the student demerit (compliance.Penalty) it escalates from —
    # an audit link from the rule breach to the money. It must be a demerit on THIS student
    # (you can't pin another student's, or a staff member's, penalty to this fine). A single
    # filter covers exists + same-student + is-a-student-penalty (staff penalties have a
    # null student_id, so they never match a concrete student_id).
    penalty_id = payload.get("penalty_id")
    if penalty_id is not None:
        from apps.compliance.models import Penalty

        if (
            not isinstance(penalty_id, int)
            or isinstance(penalty_id, bool)
            or not Penalty.objects.filter(pk=penalty_id, student_id=student_id).exists()
        ):
            raise ValidationException(
                _("penalty_id must be a demerit on the same student."),
                code="fine_penalty_invalid",
                fields={"payload": ["penalty_id"]},
            )
        clean["penalty_id"] = penalty_id
    return clean


def _validate_absence_deduction_payload(payload: dict) -> dict:
    """Validate + normalize an absence-deduction payload at creation time. Shape:

        {student_id, attendance_id, fixed_amount_uzs}

    The center must have opted into the policy (CenterSettings.absence_deduction_enabled),
    the referenced attendance record must be a real absence for THIS student, and — when
    the center restricts to excused absences — it must be EXCUSED (carry an accepted
    reason). A given absence can be deducted only once (anti-fraud: no double credit for
    one missed lesson). The deduction amount is the missed lesson's worth the manager
    specifies; it materializes as a standing finance.Discount on approval.
    """
    from apps.attendance.models import AttendanceRecord
    from apps.org.selectors import get_center_settings
    from apps.students.models import StudentProfile

    settings_obj = get_center_settings()
    if not settings_obj.absence_deduction_enabled:
        raise ValidationException(
            _("This center does not allow absence deductions."), code="absence_deduction_disabled"
        )

    student_id = payload.get("student_id")
    if (
        not isinstance(student_id, int)
        or isinstance(student_id, bool)
        or not StudentProfile.objects.filter(pk=student_id).exists()
    ):
        raise ValidationException(
            _("An absence-deduction request needs a valid student_id in its payload."),
            code="absence_deduction_student_required",
            fields={"payload": ["student_id"]},
        )

    attendance_id = payload.get("attendance_id")
    record = (
        AttendanceRecord.objects.filter(pk=attendance_id, student_id=student_id).first()
        if isinstance(attendance_id, int) and not isinstance(attendance_id, bool)
        else None
    )
    absences = (AttendanceRecord.Status.ABSENT, AttendanceRecord.Status.EXCUSED)
    if record is None or record.status not in absences:
        raise ValidationException(
            _("attendance_id must reference an absence for this student."),
            code="absence_deduction_attendance_invalid",
            fields={"payload": ["attendance_id"]},
        )
    if settings_obj.absence_deduction_excused_only and record.status != AttendanceRecord.Status.EXCUSED:
        raise ValidationException(
            _("This center only deducts for excused (reasoned) absences."),
            code="absence_deduction_requires_excuse",
        )

    # Anti-fraud: one deduction per absence. A still-live (pending/approved) request for
    # the same attendance record blocks a second; a rejected/cancelled one does not (an
    # overturned deduction may be re-requested).
    if (
        ApprovalRequest.objects.filter(kind=KIND_ABSENCE_DEDUCTION, payload__attendance_id=attendance_id)
        .exclude(status__in=(ApprovalRequest.Status.REJECTED, ApprovalRequest.Status.CANCELLED))
        .exists()
    ):
        raise ValidationException(
            _("This absence has already been deducted."), code="absence_deduction_duplicate"
        )

    # The deduction value (the missed lesson's worth) — same never-500 discipline as the
    # discount fixed amount: finite, positive, within NUMERIC(18,2), re-checked after the
    # quantize that could round up across the boundary.
    try:
        fv = Decimal(str(payload.get("fixed_amount_uzs")))
    except (InvalidOperation, ValueError):
        raise ValidationException(
            _("fixed_amount_uzs must be a number."), code="absence_deduction_amount_invalid"
        ) from None
    if not fv.is_finite():
        raise ValidationException(
            _("fixed_amount_uzs must be a finite number."), code="absence_deduction_amount_invalid"
        )
    if fv <= 0:
        raise ValidationException(
            _("fixed_amount_uzs must be positive."), code="absence_deduction_amount_range"
        )
    if fv >= Decimal("1e16"):
        raise ValidationException(_("fixed_amount_uzs is too large."), code="absence_deduction_amount_range")
    fv = fv.quantize(_TWO_PLACES)
    if fv >= Decimal("1e16"):
        raise ValidationException(_("fixed_amount_uzs is too large."), code="absence_deduction_amount_range")
    return {"student_id": student_id, "attendance_id": attendance_id, "fixed_amount_uzs": str(fv)}


@transaction.atomic
def create_request(
    *,
    kind: str,
    title: str,
    requested_by=None,
    amount_uzs: Decimal | None = None,
    description: str = "",
    branch=None,
    payload: dict | None = None,
) -> ApprovalRequest:
    payload = payload or {}
    # The serializer's JSONField accepts any JSON value (a string/array/number is valid
    # JSON), so a non-object payload would reach a kind validator's .get() and 500 with an
    # AttributeError. Reject a non-object payload here as a clean 400 for every kind.
    if not isinstance(payload, dict):
        raise ValidationException(_("payload must be a JSON object."), code="payload_invalid")
    if kind == KIND_DISCOUNT:
        # A discount is decision-only (the Discount it grants is the effect, not a
        # cash payout) — it never disburses, so drop any amount the caller passed.
        payload = _validate_discount_payload(payload)
        amount_uzs = None
    elif kind == KIND_PAYMENT_DELAY:
        # Also decision-only: the effect is moving a due date, not paying money out.
        payload = _validate_payment_delay_payload(payload)
        amount_uzs = None
    elif kind == KIND_LOAN:
        # Money-moving: a loan must carry the amount to be paid out, and a borrower.
        if amount_uzs is None:
            raise ValidationException(_("A loan request must have an amount."), code="loan_amount_required")
        payload = {**(payload or {}), **_validate_loan_payload(payload)}
    elif kind == KIND_FINE:
        # Decision-only: the effect is a charge the student owes, not a cash payout.
        # Fold the top-level amount into the payload and null the request amount so it
        # can never be disbursed (disburse pays money OUT; a fine collects money IN).
        payload = _validate_fine_payload({**(payload or {}), "amount_uzs": amount_uzs})
        amount_uzs = None
    elif kind == KIND_ABSENCE_DEDUCTION:
        # Decision-only: the effect is a credit (a standing Discount), not a cash payout.
        payload = _validate_absence_deduction_payload(payload)
        amount_uzs = None
    elif kind == KIND_SALARY_PREP:
        # Money-OUT to a teacher: the amount is the computed payout and the payload must
        # pin the beneficiary teacher's User id (SoD extends to them) + the payee label.
        if amount_uzs is None:
            raise ValidationException(
                _("A salary request must have a computed amount."), code="salary_amount_required"
            )
        payload = _validate_salary_prep_payload(payload)
    return ApprovalRequest.objects.create(
        kind=kind,
        title=title,
        requested_by=requested_by,
        amount_uzs=amount_uzs,
        description=description,
        branch=branch,
        payload=payload,
    )


def _apply_discount_effect(req: ApprovalRequest, actor) -> None:
    """On approval, a discount request materializes a standing Discount for the
    student — which finance then auto-applies as a negative invoice line at the
    next issue (apps.finance._active_discounts). Runs inside approve()'s
    transaction, so a failed effect rolls the approval back. The created discount
    id is stamped into the payload as the audit link."""
    from apps.finance.models import Discount
    from apps.students.models import StudentProfile

    p = dict(req.payload or {})
    if p.get("discount_id"):  # defensive: status gate already prevents re-approval
        return
    student_id = p.get("student_id")
    if not student_id or not StudentProfile.objects.filter(pk=student_id).exists():
        raise UnprocessableEntity(
            _("The discount's student no longer exists."), code="discount_student_missing"
        )
    discount = Discount.objects.create(
        student_id=student_id,
        discount_type=p.get("discount_type", Discount.DiscountType.MANUAL),
        percent=Decimal(p["percent"]) if p.get("percent") is not None else None,
        fixed_amount_uzs=Decimal(p["fixed_amount_uzs"]) if p.get("fixed_amount_uzs") is not None else None,
        valid_from=p.get("valid_from") or None,
        valid_until=p.get("valid_until") or None,
        approved_by=actor,
    )
    req.payload = {**p, "discount_id": discount.pk}


def _apply_fine_effect(req: ApprovalRequest, actor) -> None:
    """On approval, a fine request materializes a one-off PENALTY invoice the student
    must pay — collected through the normal payments/allocation machinery (it can go
    overdue, be refunded, etc.). Runs inside approve()'s transaction, so a failed
    effect rolls the approval back. The issued invoice id/number is stamped into the
    payload as the audit link. Discounts are deliberately NOT applied: a scholarship
    must not shrink a punishment."""
    from apps.finance.models import InvoiceLine
    from apps.finance.services import issue_invoice
    from apps.students.models import StudentProfile

    p = dict(req.payload or {})
    if p.get("invoice_id"):  # defensive: the status gate already prevents re-approval
        return
    student_id = p.get("student_id")
    if not student_id or not StudentProfile.objects.filter(pk=student_id).exists():
        raise UnprocessableEntity(_("The fine's student no longer exists."), code="fine_student_missing")
    invoice = issue_invoice(
        student_id=student_id,
        lines=[
            {
                "description": (p.get("reason") or _("Fine")),
                "line_type": InvoiceLine.LineType.PENALTY,
                "quantity": "1",
                "unit_price_uzs": p["amount_uzs"],
            }
        ],
        created_by=actor,
        apply_discounts=False,
    )
    req.payload = {**p, "invoice_id": invoice.pk, "invoice_number": invoice.number}


def _apply_absence_deduction_effect(req: ApprovalRequest, actor) -> None:
    """On approval, an absence-deduction materializes a SINGLE-USE finance.Discount that
    credits the student for the one lesson they missed — finance applies it to the next
    invoice and then retires it (single_use), so one missed lesson is credited exactly once
    and never recurs on later bills. Runs inside approve()'s transaction so a failed effect
    rolls the approval back. The attendance row is locked first so two requests for the
    same absence can't both approve (write-skew → double credit); the approve-time re-check
    then rejects the loser. The created discount id is stamped back as the audit link."""
    from apps.attendance.models import AttendanceRecord
    from apps.finance.models import Discount
    from apps.students.models import StudentProfile

    p = dict(req.payload or {})
    if p.get("discount_id"):  # defensive: the status gate already prevents re-approval
        return
    student_id = p.get("student_id")
    if not student_id or not StudentProfile.objects.filter(pk=student_id).exists():
        raise UnprocessableEntity(
            _("The deduction's student no longer exists."), code="absence_deduction_student_missing"
        )
    attendance_id = p["attendance_id"]  # always present (validated at creation)
    # Serialize concurrent approvals for the same absence on the attendance row, so the
    # "already deducted?" check below is race-free (no write-skew double credit).
    record = AttendanceRecord.objects.select_for_update().filter(pk=attendance_id).first()
    # Re-assert the create-time invariant at APPROVE time: the record must STILL be an
    # ABSENT/EXCUSED absence. Between request and approval the register can be corrected
    # (absent -> present); approving a now-corrected record would credit the student for
    # a lesson they attended. (clear_absence_deduction only retires an ALREADY-APPROVED
    # credit, so a PENDING request corrected before approval needs this guard.)
    if record is None or record.status not in (
        AttendanceRecord.Status.ABSENT,
        AttendanceRecord.Status.EXCUSED,
    ):
        raise UnprocessableEntity(
            _("The cited attendance record is no longer an absence."),
            code="absence_deduction_attendance_invalid",
        )
    # Re-assert the excused-only policy too (mirrors create-time): if the excuse was
    # revoked (EXCUSED -> ABSENT) between request and approval, an excused-only center
    # must not credit the now-unexcused absence.
    from apps.org.selectors import get_center_settings

    if (
        get_center_settings().absence_deduction_excused_only
        and record.status != AttendanceRecord.Status.EXCUSED
    ):
        raise UnprocessableEntity(
            _("This center only deducts for excused (reasoned) absences."),
            code="absence_deduction_requires_excuse",
        )
    if (
        ApprovalRequest.objects.filter(
            kind=KIND_ABSENCE_DEDUCTION,
            status=ApprovalRequest.Status.APPROVED,
            payload__attendance_id=attendance_id,
        )
        .exclude(pk=req.pk)
        .exists()
    ):
        raise UnprocessableEntity(
            _("This absence has already been deducted."), code="absence_deduction_duplicate"
        )
    discount = Discount.objects.create(
        student_id=student_id,
        discount_type=Discount.DiscountType.MANUAL,
        fixed_amount_uzs=Decimal(p["fixed_amount_uzs"]),
        approved_by=actor,
        # One-time: the credit is for ONE missed lesson, so it applies to a single invoice
        # then retires (it must not recur on every future bill like a standing scholarship).
        single_use=True,
    )
    req.payload = {**p, "discount_id": discount.pk}


@transaction.atomic
def clear_absence_deduction(*, attendance_id: int, actor=None) -> bool:
    """Retire the single-use credit an approved absence_deduction materialized, when
    that absence is later CORRECTED (the student was actually present). Without this,
    a post-approval attendance fix (absent -> present) leaves the standing single-use
    Discount active and finance credits the student for a lesson they attended, with no
    trail linking the loss back to the correction.

    No-op when there is no approved deduction for this attendance, or its credit was
    already applied + retired (is_active=False) — reversing an already-billed credit
    needs an invoice adjustment, which is out of scope here. Returns True iff a
    still-active (unclaimed) credit was retired. Called by attendance.mark_attendance
    on an absent->present transition (lazy import to avoid a hard app dependency)."""
    from apps.finance.models import Discount

    req = ApprovalRequest.objects.filter(
        kind=KIND_ABSENCE_DEDUCTION,
        status=ApprovalRequest.Status.APPROVED,
        payload__attendance_id=attendance_id,
    ).first()
    if req is None:
        return False
    discount_id = (req.payload or {}).get("discount_id")
    if not discount_id:
        return False
    # Conditional UPDATE: only retire a still-active credit (matches _build_discount_lines'
    # single_use consumption). An already-consumed credit is left as-is.
    retired = bool(Discount.objects.filter(pk=discount_id, is_active=True).update(is_active=False))
    if retired:
        req.payload = {**(req.payload or {}), "credit_cleared": "attendance_corrected"}
        req.save(update_fields=["payload", "updated_at"])
    return retired


def _apply_payment_delay_effect(req: ApprovalRequest, actor) -> None:
    """On approval, a payment-delay request pushes its target invoice's due date
    via the finance service (which re-validates + un-overdues atomically). The
    prior due date/status are snapshotted (so a later rejection can restore them)
    and the applied date/status are stamped into the payload as the audit trail."""
    from apps.finance.models import Invoice
    from apps.finance.services import extend_invoice_due_date

    p = dict(req.payload or {})
    before = Invoice.objects.filter(pk=p["invoice_id"]).only("due_date", "status").first()
    previous_due = before.due_date.isoformat() if before and before.due_date else None
    previous_status = before.status if before else None
    invoice = extend_invoice_due_date(
        invoice_id=p["invoice_id"],
        new_due_date=date.fromisoformat(p["new_due_date"]),
        actor=actor,
    )
    req.payload = {
        **p,
        "previous_due_date": previous_due,
        "previous_status": previous_status,
        "applied_due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "invoice_status": invoice.status,
    }


def _apply_approval_effect(req: ApprovalRequest, actor) -> None:
    """Dispatch the kind-specific side-effect that fires the instant a request is
    APPROVED. Money-moving kinds (loan/expense/...) act at disburse time instead;
    decision kinds with an effect (discount, payment_delay) act here."""
    if req.kind == KIND_DISCOUNT:
        _apply_discount_effect(req, actor)
    elif req.kind == KIND_FINE:
        _apply_fine_effect(req, actor)
    elif req.kind == KIND_ABSENCE_DEDUCTION:
        _apply_absence_deduction_effect(req, actor)
    elif req.kind == KIND_PAYMENT_DELAY:
        _apply_payment_delay_effect(req, actor)


def _reverse_discount_effect(req: ApprovalRequest) -> None:
    """Deactivate the granted Discount so it stops auto-applying — a rejected price
    cut must not keep cutting prices."""
    from apps.finance.models import Discount

    p = dict(req.payload or {})
    discount_id = p.get("discount_id")
    if discount_id:
        Discount.objects.filter(pk=discount_id).update(is_active=False)
        req.payload = {**p, "effect_reversed": True}


def _reverse_fine_effect(req: ApprovalRequest) -> None:
    """Void the penalty invoice so an overturned fine stops owing money. If the
    student already paid (or part-paid) it, void_invoice raises a clean 409 — the
    whole reject() rolls back, forcing the manager to use the refund flow instead of
    silently un-billing collected money (anti-fraud: money already moved stays
    traceable)."""
    from apps.finance.models import Invoice
    from apps.finance.services import void_invoice

    p = dict(req.payload or {})
    invoice_id = p.get("invoice_id")
    if not invoice_id:
        return
    invoice = Invoice.objects.filter(pk=invoice_id).first()
    if invoice is not None and invoice.status != Invoice.Status.VOID:
        void_invoice(invoice=invoice)
    req.payload = {**p, "effect_reversed": True}


def _reverse_absence_deduction_effect(req: ApprovalRequest) -> None:
    """Deactivate the credit Discount so an overturned deduction stops crediting — and the
    absence becomes deductible again (a fresh request is no longer blocked as a duplicate,
    since rejected requests are excluded from the duplicate guard)."""
    from apps.finance.models import Discount

    p = dict(req.payload or {})
    discount_id = p.get("discount_id")
    if discount_id:
        Discount.objects.filter(pk=discount_id).update(is_active=False)
        req.payload = {**p, "effect_reversed": True}


def _reverse_payment_delay_effect(req: ApprovalRequest, actor) -> None:
    """Put the invoice's due date back to its pre-extension value (snapshotted at
    approve time), re-flagging OVERDUE if appropriate."""
    from apps.finance.services import restore_invoice_due_date

    p = dict(req.payload or {})
    invoice_id = p.get("invoice_id")
    if invoice_id and "previous_due_date" in p:
        prev = p["previous_due_date"]
        restore_invoice_due_date(
            invoice_id=invoice_id,
            due_date=date.fromisoformat(prev) if prev else None,
            actor=actor,
        )
        req.payload = {**p, "effect_reversed": True}


def _reverse_approval_effect(req: ApprovalRequest, actor) -> None:
    """Compensate the on-approval side-effect when an already-APPROVED request is
    overturned (rejected). Money-moving kinds need no reversal here — they only act
    at disburse. Runs inside reject()'s transaction so the undo is atomic."""
    if req.kind == KIND_DISCOUNT:
        _reverse_discount_effect(req)
    elif req.kind == KIND_FINE:
        _reverse_fine_effect(req)
    elif req.kind == KIND_ABSENCE_DEDUCTION:
        _reverse_absence_deduction_effect(req)
    elif req.kind == KIND_PAYMENT_DELAY:
        _reverse_payment_delay_effect(req, actor)


def _locked(request_id: int) -> ApprovalRequest:
    req = ApprovalRequest.objects.select_for_update().filter(pk=request_id).first()
    if req is None:
        raise NotFoundException(_("Approval request not found."), code="approval_not_found")
    return req


def _assert_not_self_approval(req: ApprovalRequest, actor) -> None:
    """Segregation of duties / maker-checker: the person who raised a request may
    never sign it off (anti-fraud DNA — "no untracked favours"). Enforced in the
    service so every caller is covered, not just the view. Superusers are exempt."""
    if actor is None or getattr(actor, "is_superuser", False):
        return
    if req.requested_by_id and req.requested_by_id == getattr(actor, "id", None):
        raise PermissionException(_("You cannot approve your own request."), code="self_approval")


# Money-OUT-to-a-named-STAFF-member kinds pin the beneficiary identity in the
# payload under a per-kind key. SoD extends to that beneficiary — they may neither
# approve nor disburse a payout to themselves. Each entry: (payload key, error code,
# message). Supplier/vendor payees (procurement/expense party_label) and student
# payees (book_cash money-IN) are NOT staff users, so they aren't listed.
_BENEFICIARY_SELF_DEALING: dict[str, tuple[str, str, Any]] = {
    KIND_LOAN: ("borrower_id", "loan_self_dealing", _("You cannot approve or disburse your own loan.")),
    KIND_REWARD: (
        "recipient_id",
        "reward_self_dealing",
        _("You cannot approve or disburse your own reward."),
    ),
    KIND_SALARY_PREP: (
        "teacher_profile_id",
        "salary_self_dealing",
        _("You cannot approve or disburse your own salary."),
    ),
}


def _assert_not_beneficiary_self_dealing(req: ApprovalRequest, actor) -> None:
    """Segregation of duties extends to the BENEFICIARY, not just the maker: the named
    payee of a money-OUT request (a loan borrower, a cash-reward recipient) may neither
    approve nor disburse their own payout. Without this, a colleague keys the request
    naming the beneficiary, and the beneficiary (if they hold approve/disburse rights)
    signs off the payout to themselves — the requester self-approval block alone misses
    it. Applied on BOTH approve and disburse. Superusers are exempt."""
    if actor is None or getattr(actor, "is_superuser", False):
        return
    spec = _BENEFICIARY_SELF_DEALING.get(req.kind)
    if spec is None:
        return
    key, code, message = spec
    # Coerce the pinned beneficiary id to int before comparing: the SoD identity must match the
    # actor's pk regardless of how it was stored, so a string "5" cannot slip past the guard for
    # actor 5 (a type-confusion bypass). A missing/non-coercible id is nobody's pk, so it never
    # blocks.
    raw = req.payload.get(key)
    try:
        parsed_beneficiary_id = int(raw)
    except (TypeError, ValueError):
        return
    beneficiary_id: int | None = parsed_beneficiary_id
    if req.kind == KIND_SALARY_PREP:
        from apps.teachers.models import TeacherProfile

        beneficiary_id = (
            TeacherProfile.objects.filter(pk=parsed_beneficiary_id).values_list("user_id", flat=True).first()
        )
    if beneficiary_id == getattr(actor, "id", None):
        raise PermissionException(message, code=code)


@transaction.atomic
def approve(*, request_id: int, actor=None, note: str = "") -> ApprovalRequest:
    req = _locked(request_id)
    if req.status != ApprovalRequest.Status.PENDING:
        raise UnprocessableEntity(_("Only a pending request can be approved."), code="approval_not_pending")
    _assert_not_self_approval(req, actor)
    _assert_not_beneficiary_self_dealing(req, actor)
    req.status = ApprovalRequest.Status.APPROVED
    req.decided_by = actor
    req.decided_at = timezone.now()
    req.decision_note = note
    # Side-effect (e.g. discount -> standing Discount) runs in this same transaction
    # and may stamp req.payload, so persist payload alongside the decision fields.
    _apply_approval_effect(req, actor)
    req.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "payload", "updated_at"])
    _notify(event_type="approval.approved", recipient_id=req.requested_by_id, req=req)
    if req.amount_uzs is not None:
        # Tell whoever can pay it out that money is ready to be readied (PRODUCT_VISION
        # "cashier auto-notified to ready the money").
        for uid in _disburser_ids(req):
            _notify(event_type="approval.awaiting_disbursement", recipient_id=uid, req=req)
    return req


@transaction.atomic
def reject(*, request_id: int, actor=None, note: str = "") -> ApprovalRequest:
    req = _locked(request_id)
    if req.status not in (ApprovalRequest.Status.PENDING, ApprovalRequest.Status.APPROVED):
        raise UnprocessableEntity(
            _("This request can no longer be rejected."), code="approval_not_rejectable"
        )
    was_approved = req.status == ApprovalRequest.Status.APPROVED
    req.status = ApprovalRequest.Status.REJECTED
    req.decided_by = actor
    req.decided_at = timezone.now()
    req.decision_note = note
    if was_approved:
        # Overturning an approval whose effect already fired (discount / payment_delay)
        # must undo that effect, atomically, or a "rejected" decision still bites.
        _reverse_approval_effect(req, actor)
    req.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "payload", "updated_at"])
    _notify(event_type="approval.rejected", recipient_id=req.requested_by_id, req=req)
    return req


@transaction.atomic
def cancel(*, request_id: int, actor=None) -> ApprovalRequest:
    """Requester withdraws a still-pending request (ownership enforced by the view)."""
    req = _locked(request_id)
    if req.status != ApprovalRequest.Status.PENDING:
        raise UnprocessableEntity(
            _("Only a pending request can be cancelled."), code="approval_not_cancellable"
        )
    req.status = ApprovalRequest.Status.CANCELLED
    req.decided_by = actor
    req.decided_at = timezone.now()
    req.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    return req


@transaction.atomic
def disburse(
    *,
    request_id: int,
    payment_method_id: int,
    actor=None,
    direction: str = LedgerEntry.Direction.OUT,
    entry_type: str = "",
    party_label: str = "",
) -> ApprovalRequest:
    """Pay out an APPROVED, amount-bearing request: writes one immutable LedgerEntry
    and links it. Idempotency is guaranteed by the status gate (a DISBURSED request
    can't be disbursed again)."""
    from apps.finance.models import PaymentMethod

    req = _locked(request_id)
    if req.status != ApprovalRequest.Status.APPROVED:
        raise UnprocessableEntity(
            _("Only an approved request can be disbursed."), code="approval_not_approved"
        )
    _assert_not_beneficiary_self_dealing(req, actor)
    if req.amount_uzs is None:
        raise UnprocessableEntity(_("This request has no amount to disburse."), code="approval_no_amount")
    method = PaymentMethod.objects.filter(pk=payment_method_id, is_active=True).first()
    if method is None:
        raise UnprocessableEntity(_("Unknown or inactive payment method."), code="payment_method_invalid")

    pinned_payee = req.payload.get("party_label")
    if pinned_payee:
        # A request that pre-designated its payee (loan borrower / procurement
        # supplier / reward recipient) gets an IMMUTABLE ledger row: the disburser
        # cannot silently substitute who got paid, flip the sign, or recategorise it
        # away from the approved kind. The payee, money-OUT direction, and entry_type
        # are fixed by the approved request, not the cashier (anti-fraud DNA).
        party_label = pinned_payee
        direction = LedgerEntry.Direction.OUT
        entry_type = req.kind

    entry = LedgerEntry.objects.create(
        direction=direction,
        entry_type=entry_type or req.kind,
        amount_uzs=req.amount_uzs,
        branch=req.branch,
        # For a pinned-payee kind the payload payee already won above; otherwise an
        # explicit label wins, else fall back to the requester. Truncated to the
        # column width (varchar(200)) — a long full name must not surface as a DB 500.
        party_label=(party_label or (req.requested_by.get_full_name() if req.requested_by else ""))[:200],
        payment_method=method,
        source_kind="approval_request",
        source_id=req.pk,
        note=req.title[:255],
        created_by=actor,
    )
    req.status = ApprovalRequest.Status.DISBURSED
    req.disbursed_by = actor
    req.disbursed_at = timezone.now()
    req.payment_method = method
    req.ledger_entry = entry
    req.save(
        update_fields=[
            "status",
            "disbursed_by",
            "disbursed_at",
            "payment_method",
            "ledger_entry",
            "updated_at",
        ]
    )
    _notify(event_type="approval.disbursed", recipient_id=req.requested_by_id, req=req)
    return req
