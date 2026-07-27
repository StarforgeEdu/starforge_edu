"""Cards (F12-1): student access/ID cards + scan check-in.

A manager defines `CardType`s; a `Card` is issued to a student carrying a unique scan
`code` (the QR/NFC payload). Scanning the code records a `CardScan` (the digital
check-in log — kill the paper sign-in sheet at the door) and reports whether the card
was VALID, so a revoked/lost card is rejected. The stored-value wallet is a later slice.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.utils.translation import gettext_lazy as _


class CardType(models.Model):
    """A named kind of card a center issues — e.g. 'Student ID', 'Access pass'. Managers
    create + name them; `is_active=False` retires a type without erasing issued cards."""

    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class Card(models.Model):
    """A card issued to a student, carrying a unique scan `code`. Scanning the code checks
    the student in. A revoked (is_active=False) card scans as INVALID — lost-card safety."""

    student = models.ForeignKey("students.StudentProfile", on_delete=models.PROTECT, related_name="cards")
    card_type = models.ForeignKey(CardType, on_delete=models.PROTECT, related_name="cards")
    code = models.CharField(max_length=64, unique=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    issued_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    issued_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("-issued_at",)
        indexes = [models.Index(fields=("student", "is_active"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"card#{self.pk}:s{self.student_id}:{'active' if self.is_active else 'revoked'}"


class CardScan(models.Model):
    """A scan event = the check-in log. Every scan is recorded (even an invalid one — the
    audit trail of who tried a revoked/lost card), with who scanned + whether it was valid."""

    card = models.ForeignKey(Card, on_delete=models.PROTECT, related_name="scans")
    scanned_at = models.DateTimeField(auto_now_add=True, db_index=True)
    scanned_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    was_valid = models.BooleanField()  # the card's active state AT scan time
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("-scanned_at",)
        indexes = [models.Index(fields=("card", "scanned_at"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"scan#{self.pk}:c{self.card_id}:{'ok' if self.was_valid else 'invalid'}"


class Wallet(models.Model):
    """A student's stored-value balance (F12-1) — load money in, spend it at the
    canteen/shop (kill cash). The `balance_uzs` is the running total of its append-only
    `WalletTransaction`s; it is mutated only under a row lock so two concurrent spends
    can't overdraw it. A CheckConstraint backstops the never-negative invariant."""

    student = models.OneToOneField("students.StudentProfile", on_delete=models.PROTECT, related_name="wallet")
    balance_uzs = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(balance_uzs__gte=Decimal("0")), name="wallet_balance_non_negative"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"wallet:s{self.student_id}:{self.balance_uzs}"


class WalletTransaction(models.Model):
    """An append-only entry on a wallet (the audit trail of every load + spend). `amount_uzs`
    is always positive; `kind` gives the direction. `balance_after_uzs` snapshots the
    running balance so a statement reads straight off the ledger."""

    class Kind(models.TextChoices):
        TOPUP = "topup", _("Top up")
        SPEND = "spend", _("Spend")
        REFUND = "refund", _("Refund")

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="transactions")
    kind = models.CharField(max_length=8, choices=Kind.choices, db_index=True)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    balance_after_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("wallet", "created_at"))]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_uzs__gt=Decimal("0")), name="wallet_txn_amount_positive"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"wtxn#{self.pk}:{self.kind}:{self.amount_uzs}"
