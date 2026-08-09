"""Cards services (F12-1): define card types, issue / revoke cards, scan to check in."""

from __future__ import annotations

import json
import logging
import secrets
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.cards.models import Card, CardScan, CardType, Wallet, WalletTransaction
from core.exceptions import (
    ConflictException,
    NotFoundException,
    PermissionException,
    UnprocessableEntity,
    ValidationException,
)
from core.role_principals import RolePrincipal
from core.utils import current_schema, stable_hash

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_IDEMPOTENCY_KEY_MIN_LENGTH = 16
_IDEMPOTENCY_KEY_MAX_LENGTH = 128


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
def scan_card(
    *,
    code: str,
    scanned_by=None,
    note: str = "",
    is_unscoped: bool = True,
    branch_ids: set[int] | None = None,
) -> dict:
    """Scan a card code to check a student in. An unknown code is a clean 404; a known
    code is ALWAYS logged (even a revoked card — the audit trail of an attempted entry),
    and the result reports whether the card was valid + who it belongs to."""
    card = Card.objects.select_related("student__user", "card_type").filter(code=(code or "").strip()).first()
    if card is None:
        raise NotFoundException(_("No card matches that code."), code="card_not_found")
    if not is_unscoped and card.student.branch_id not in (branch_ids or set()):
        # Match an unknown code so a scanner cannot use the endpoint as a
        # cross-branch card/student existence oracle.
        raise NotFoundException(_("No card matches that code."), code="card_not_found")
    valid = card.is_active
    scan = CardScan.objects.create(
        card=card,
        scanned_by=scanned_by,
        was_valid=valid,
        note=(note or "").strip()[:255],
    )
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
        "note": scan.note,
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


def _post(
    wallet: Wallet,
    *,
    kind: str,
    amount: Decimal,
    actor,
    principal: RolePrincipal,
    note: str,
    idempotency_key_hash: str,
    operation_fingerprint: str,
) -> WalletTransaction:
    wallet.save(update_fields=["balance_uzs", "updated_at"])
    return WalletTransaction.objects.create(
        wallet=wallet,
        kind=kind,
        amount_uzs=amount,
        balance_after_uzs=wallet.balance_uzs,
        created_by=actor,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        idempotency_key_hash=idempotency_key_hash,
        operation_fingerprint=operation_fingerprint,
        note=(note or "")[:255],
    )


def validate_wallet_idempotency_key(raw: str | None) -> str:
    """Return one canonical client retry key without ever normalizing ambiguity away."""

    if (
        not isinstance(raw, str)
        or not _IDEMPOTENCY_KEY_MIN_LENGTH <= len(raw) <= _IDEMPOTENCY_KEY_MAX_LENGTH
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in raw)
    ):
        raise ValidationException(
            _("Idempotency-Key must contain 16 to 128 visible ASCII characters."),
            code="invalid_idempotency_key",
            fields={"Idempotency-Key": [_("Use 16 to 128 visible ASCII characters.")]},
        )
    return raw


def _wallet_key_hash(*, principal: RolePrincipal, raw: str) -> str:
    return stable_hash(f"wallet-key:v1:{current_schema()}:{principal.kind}:{principal.principal_id}:{raw}")


def _wallet_operation_fingerprint(
    *,
    kind: str,
    student_id: int,
    amount: Decimal,
    note: str,
) -> str:
    payload = {
        "amount_uzs": str(amount),
        "kind": kind,
        "note": note,
        "student_id": student_id,
    }
    return stable_hash(
        "wallet-operation:v1:"
        + json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )


def _lock_wallet_operation(*, principal: RolePrincipal, key_hash: str) -> None:
    """Serialize one principal/key before any balance mutation.

    The unique constraint is the durable backstop.  The transaction-scoped advisory
    lock also makes a concurrent retry return the committed winner instead of first
    executing a second balance change and then colliding at insert time.
    """

    lock_hash = stable_hash(
        f"wallet-lock:v1:{current_schema()}:{principal.kind}:{principal.principal_id}:{key_hash}"
    )
    lock_id = int(lock_hash[:15], 16)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def _existing_wallet_operation(
    *,
    principal: RolePrincipal,
    key_hash: str,
    operation_fingerprint: str,
) -> WalletTransaction | None:
    existing = WalletTransaction.objects.filter(idempotency_key_hash=key_hash).first()
    if existing is None:
        return None
    if (
        existing.actor_principal_kind != principal.kind
        or existing.actor_principal_id != principal.principal_id
        or existing.operation_fingerprint != operation_fingerprint
    ):
        raise ConflictException(
            _("This idempotency key belongs to a different wallet operation."),
            code="idempotency_key_reused",
        )
    return existing


def _assert_actor_matches_principal(*, actor, principal: RolePrincipal) -> None:
    if getattr(actor, "pk", None) != principal.user_id:
        raise PermissionException(
            _("This wallet operation is unavailable for the active account session."),
            code="principal_unavailable",
        )


def _locked_student_in_scope(*, student_id: int, is_unscoped: bool, branch_ids: set[int]):
    """Lock and reauthorize the student's current placement inside the money transaction."""

    from apps.students.models import StudentProfile

    student = StudentProfile.objects.select_for_update().only("pk", "branch_id").filter(pk=student_id).first()
    if student is None or (not is_unscoped and student.branch_id not in branch_ids):
        # Absent and out-of-scope identifiers are intentionally indistinguishable.
        raise NotFoundException(_("Student not found."), code="student_not_found")
    return student


@transaction.atomic
def _wallet_operation(
    *,
    student,
    amount,
    actor,
    principal: RolePrincipal,
    idempotency_key: str,
    is_unscoped: bool,
    branch_ids: set[int],
    note: str,
    kind: str,
) -> WalletTransaction:
    _assert_actor_matches_principal(actor=actor, principal=principal)
    amt = _clean_amount(amount)
    clean_note = (note or "")[:255]
    raw_key = validate_wallet_idempotency_key(idempotency_key)
    key_hash = _wallet_key_hash(principal=principal, raw=raw_key)
    fingerprint = _wallet_operation_fingerprint(
        kind=kind,
        student_id=student.pk,
        amount=amt,
        note=clean_note,
    )
    # The view's scope check is a fast rejection only. A student can transfer
    # concurrently, so lock and recheck current placement before replay lookup
    # or any wallet mutation.
    locked_student = _locked_student_in_scope(
        student_id=student.pk,
        is_unscoped=is_unscoped,
        branch_ids=branch_ids,
    )
    _lock_wallet_operation(principal=principal, key_hash=key_hash)
    existing = _existing_wallet_operation(
        principal=principal,
        key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    if existing is not None:
        return existing

    wallet = _locked_wallet(locked_student)
    if kind == WalletTransaction.Kind.SPEND:
        if wallet.balance_uzs < amt:
            raise UnprocessableEntity(_("Insufficient wallet balance."), code="insufficient_funds")
        wallet.balance_uzs -= amt
    else:
        new_balance = wallet.balance_uzs + amt
        # NUMERIC(18,2) ceiling: a single amount is bounded, but the CUMULATIVE
        # balance must be too — reject a clean 422 rather than a DB-overflow 500.
        if new_balance >= Decimal("1e16"):
            raise UnprocessableEntity(
                _("That would exceed the wallet's maximum balance."), code="balance_overflow"
            )
        wallet.balance_uzs = new_balance
    return _post(
        wallet,
        kind=kind,
        amount=amt,
        actor=actor,
        principal=principal,
        note=clean_note,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )


def top_up(
    *,
    student,
    amount,
    actor,
    principal: RolePrincipal,
    idempotency_key: str,
    is_unscoped: bool,
    branch_ids: set[int],
    note: str = "",
    refund: bool = False,
) -> WalletTransaction:
    """Load or refund once for an exact principal/key/body tuple."""

    return _wallet_operation(
        student=student,
        amount=amount,
        actor=actor,
        principal=principal,
        idempotency_key=idempotency_key,
        is_unscoped=is_unscoped,
        branch_ids=branch_ids,
        note=note,
        kind=WalletTransaction.Kind.REFUND if refund else WalletTransaction.Kind.TOPUP,
    )


def spend(
    *,
    student,
    amount,
    actor,
    principal: RolePrincipal,
    idempotency_key: str,
    is_unscoped: bool,
    branch_ids: set[int],
    note: str = "",
) -> WalletTransaction:
    """Charge a student's wallet (e.g. a canteen purchase). The row lock + the
    insufficient-funds check make an overdraw impossible even under concurrent spends."""

    return _wallet_operation(
        student=student,
        amount=amount,
        actor=actor,
        principal=principal,
        idempotency_key=idempotency_key,
        is_unscoped=is_unscoped,
        branch_ids=branch_ids,
        note=note,
        kind=WalletTransaction.Kind.SPEND,
    )
