"""Cards services (F12-1): define card types, issue / revoke cards, scan to check in."""

from __future__ import annotations

import logging
import secrets
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.cards.models import Card, CardScan, CardType, Wallet, WalletTransaction
from core.exceptions import NotFoundException, UnprocessableEntity

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")


@transaction.atomic
def create_card_type(*, name: str, created_by=None) -> CardType:
    return CardType.objects.create(name=name, created_by=created_by)


@transaction.atomic
def set_card_type_active(*, card_type: CardType, is_active: bool) -> CardType:
    card_type.is_active = is_active
    card_type.save(update_fields=["is_active", "updated_at"])
    return card_type


def _generate_code() -> str:
    """A unique, hard-to-guess scan payload (~22 url-safe chars = 128 bits)."""
    return secrets.token_urlsafe(16)


@transaction.atomic
def issue_card(*, student, card_type: CardType, issued_by=None) -> Card:
    """Issue a card of `card_type` to `student` with a fresh unique code. A retired card
    type can't be issued. The unique-code generation retries on the astronomically
    unlikely collision so it never surfaces as a 500."""
    if not card_type.is_active:
        raise UnprocessableEntity(_("That card type is retired."), code="card_type_inactive")
    for _attempt in range(5):
        try:
            with transaction.atomic():
                return Card.objects.create(
                    student=student, card_type=card_type, code=_generate_code(), issued_by=issued_by
                )
        except IntegrityError:
            continue  # code collision — try a new one
    raise UnprocessableEntity(_("Could not allocate a unique card code."), code="card_code_unavailable")


@transaction.atomic
def revoke_card(*, card_id: int, actor=None, reason: str = "") -> Card:
    """Deactivate a card (lost / replaced). Locked + active-only so it can't be double-
    revoked; a revoked card then scans as invalid."""
    card = Card.objects.select_for_update().filter(pk=card_id).first()
    if card is None:
        raise NotFoundException(_("Card not found."), code="card_not_found")
    if not card.is_active:
        raise UnprocessableEntity(_("This card is already revoked."), code="card_not_active")
    card.is_active = False
    card.revoked_at = timezone.now()
    card.revoke_reason = reason
    card.save(update_fields=["is_active", "revoked_at", "revoke_reason"])
    return card


@transaction.atomic
def scan_card(*, code: str, scanned_by=None) -> dict:
    """Scan a card code to check a student in. An unknown code is a clean 404; a known
    code is ALWAYS logged (even a revoked card — the audit trail of an attempted entry),
    and the result reports whether the card was valid + who it belongs to."""
    card = Card.objects.select_related("student__user", "card_type").filter(code=(code or "").strip()).first()
    if card is None:
        raise NotFoundException(_("No card matches that code."), code="card_not_found")
    valid = card.is_active
    scan = CardScan.objects.create(card=card, scanned_by=scanned_by, was_valid=valid)
    student = card.student
    # F12/F15: a valid scan feeds attendance — mark the student PRESENT on the lesson they
    # are arriving for (never overrides a teacher's mark, never creates an absence). This is
    # best-effort: an attendance hiccup must NEVER fail the door check-in, and it runs in a
    # savepoint so a DB error there can't poison the scan's transaction.
    attendance_lesson_id = None
    if valid:
        try:
            from apps.attendance.services import mark_present_from_scan

            with transaction.atomic():
                record = mark_present_from_scan(student=student, at=scan.scanned_at, marked_by=scanned_by)
            attendance_lesson_id = record.lesson_id if record is not None else None
        except Exception:
            logger.exception("card-scan attendance marking failed for student %s", student.pk)
    return {
        "scan_id": scan.pk,
        "valid": valid,
        "student": student.pk,
        "student_name": student.get_full_name(),
        "card_type": card.card_type.name,
        "scanned_at": scan.scanned_at,
        "attendance_lesson": attendance_lesson_id,
    }


# ---------------------------------------------------------------------------
# Stored-value wallet (F12-1) — load + spend, append-only, locked balance
# ---------------------------------------------------------------------------


def _clean_amount(raw) -> Decimal:
    """A positive, finite, in-range money amount — a clean 4xx on junk, never a 500 (the
    NaN/Infinity + NUMERIC(18,2) overflow class)."""
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise UnprocessableEntity(_("Amount must be a number."), code="invalid_amount") from None
    if not amount.is_finite():
        raise UnprocessableEntity(_("Amount must be a finite number."), code="invalid_amount")
    if not (Decimal("0") < amount < Decimal("1e16")):
        raise UnprocessableEntity(_("Amount is out of range."), code="amount_out_of_range")
    amount = amount.quantize(_CENT)
    if not (Decimal("0") < amount < Decimal("1e16")):  # rounding can tip 0.00x / boundary
        raise UnprocessableEntity(_("Amount is out of range."), code="amount_out_of_range")
    return amount


def _locked_wallet(student) -> Wallet:
    """The student's wallet, created on first use, then RE-FETCHED under select_for_update
    so the balance read-modify-write serialises (no concurrent overdraw / lost update)."""
    Wallet.objects.get_or_create(student=student)
    return Wallet.objects.select_for_update().get(student=student)


def _post(wallet: Wallet, *, kind, amount: Decimal, actor, note: str) -> WalletTransaction:
    wallet.save(update_fields=["balance_uzs", "updated_at"])
    return WalletTransaction.objects.create(
        wallet=wallet,
        kind=kind,
        amount_uzs=amount,
        balance_after_uzs=wallet.balance_uzs,
        created_by=actor,
        note=(note or "")[:255],
    )


@transaction.atomic
def top_up(*, student, amount, actor=None, note: str = "", refund: bool = False) -> WalletTransaction:
    """Load money onto a student's wallet (or REFUND money back). Locked balance update +
    an append-only transaction. `refund=True` records it as a refund rather than a top-up."""
    amt = _clean_amount(amount)
    wallet = _locked_wallet(student)
    new_balance = wallet.balance_uzs + amt
    # NUMERIC(18,2) ceiling: a single amount is bounded, but the CUMULATIVE balance must
    # be too — reject an overflowing total as a clean 422 rather than a DB-overflow 500.
    if new_balance >= Decimal("1e16"):
        raise UnprocessableEntity(
            _("That would exceed the wallet's maximum balance."), code="balance_overflow"
        )
    wallet.balance_uzs = new_balance
    kind = WalletTransaction.Kind.REFUND if refund else WalletTransaction.Kind.TOPUP
    return _post(wallet, kind=kind, amount=amt, actor=actor, note=note)


@transaction.atomic
def spend(*, student, amount, actor=None, note: str = "") -> WalletTransaction:
    """Charge a student's wallet (e.g. a canteen purchase). The row lock + the
    insufficient-funds check make an overdraw impossible even under concurrent spends."""
    amt = _clean_amount(amount)
    wallet = _locked_wallet(student)
    if wallet.balance_uzs < amt:
        raise UnprocessableEntity(_("Insufficient wallet balance."), code="insufficient_funds")
    wallet.balance_uzs = wallet.balance_uzs - amt
    return _post(wallet, kind=WalletTransaction.Kind.SPEND, amount=amt, actor=actor, note=note)
