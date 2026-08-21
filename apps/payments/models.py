"""Payments domain models (D3-B-1).

Provider integrations (Click/Payme/Uzum), the payment ledger, webhook intake
records, and Soliq fiscal receipts. Credentials are encrypted at rest (TD-11);
``Payment.idempotency_key`` and ``WebhookEvent(provider, event_id)`` are the two
dedupe spines (D3-B-6). Cross-lane FK to ``finance.CashierShift`` is a STRING ref
(Lane B merges after Lane A) — no Python import of the finance app here.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.fields import EncryptedCharField
from core.historical_scope import (
    ATTRIBUTED_SCOPE_STATUSES,
    ScopeAttributionStatus,
    guard_immutable_scope_snapshot,
)


class Provider(models.TextChoices):
    CLICK = "click", _("Click")
    PAYME = "payme", _("Payme")
    UZUM = "uzum", _("Uzum")


_EXTERNAL_PAYMENT_METHODS = ("click", "payme", "uzum")


class ProviderConfig(models.Model):
    """Per-tenant provider credentials. One row per provider (unique). Credential
    fields are EncryptedChar (TD-11) and write-only in the serializer."""

    provider = models.CharField(max_length=8, choices=Provider.choices)
    is_active = models.BooleanField(default=True)

    click_service_id = models.CharField(max_length=64, blank=True)
    click_merchant_id = models.CharField(max_length=64, blank=True)
    click_secret_key = EncryptedCharField(max_length=255, blank=True)

    payme_merchant_id = models.CharField(max_length=64, blank=True)
    payme_key = EncryptedCharField(max_length=255, blank=True)
    payme_test_key = EncryptedCharField(max_length=255, blank=True)

    uzum_merchant_id = models.CharField(max_length=64, blank=True)
    uzum_api_key = EncryptedCharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("provider",)
        constraints = [
            models.UniqueConstraint(fields=("provider",), name="providerconfig_one_per_provider"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.provider}{'' if self.is_active else ' (inactive)'}"


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", _("Cash")
        CLICK = "click", _("Click")
        PAYME = "payme", _("Payme")
        UZUM = "uzum", _("Uzum")
        BANK_TRANSFER = "bank_transfer", _("Bank transfer")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        PROCESSING = "processing", _("Processing")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")
        REFUNDED = "refunded", _("Refunded")

    class Allocation(models.TextChoices):
        AUTO = "auto", _("Auto")
        MANUAL_REVIEW = "manual_review", _("Manual review")
        ALLOCATED = "allocated", _("Allocated")

    provider = models.CharField(max_length=16, choices=Method.choices, db_index=True)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, default="UZS")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    idempotency_key = models.CharField(max_length=64, unique=True)
    provider_txn_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_state = models.SmallIntegerField(null=True, blank=True)  # Payme 1/2/-1/-2
    provider_created_at_ms = models.BigIntegerField(null=True, blank=True)
    cancel_reason = models.SmallIntegerField(null=True, blank=True)
    account_ref = models.CharField(max_length=64, blank=True)  # what the payer entered (e.g. invoice number)
    allocation_status = models.CharField(max_length=16, choices=Allocation.choices, default=Allocation.AUTO)
    cashier_shift = models.ForeignKey(
        "finance.CashierShift", on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    branch_at_payment = models.ForeignKey(
        "org.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    department_at_payment = models.ForeignKey(
        "org.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    attribution_status = models.CharField(
        max_length=12,
        choices=ScopeAttributionStatus.choices,
        default=ScopeAttributionStatus.UNRESOLVED,
        db_index=True,
    )
    payer = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("provider", "status")),
            models.Index(fields=("status", "created_at")),
            # The default payments log is newest-first and usually unfiltered; the
            # (status, created_at) composite can't serve the ordering without a status
            # filter. Payment is one row per transaction (high volume) — index the sort.
            models.Index(fields=("-created_at", "id"), name="payment_created_idx"),
            models.Index(fields=("account_ref",), name="payment_account_ref_idx"),
            models.Index(fields=("branch_at_payment", "paid_at"), name="payment_branch_paid_idx"),
            models.Index(
                fields=("branch_at_payment", "created_at"),
                name="payment_branch_created_idx",
            ),
            models.Index(
                fields=("department_at_payment", "paid_at"),
                name="payment_dept_paid_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(currency="UZS"),
                name="payment_currency_uzs",
            ),
            models.UniqueConstraint(
                fields=("provider", "provider_txn_id"),
                condition=(models.Q(provider__in=_EXTERNAL_PAYMENT_METHODS) & ~models.Q(provider_txn_id="")),
                name="payment_provider_txn_unique",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
                        branch_at_payment__isnull=False,
                    )
                    | models.Q(
                        attribution_status__in=(
                            ScopeAttributionStatus.UNRESOLVED,
                            ScopeAttributionStatus.CONFLICTING,
                            ScopeAttributionStatus.QUARANTINED,
                        ),
                        branch_at_payment__isnull=True,
                        department_at_payment__isnull=True,
                    )
                ),
                name="payment_scope_attribution_valid",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.provider}:{self.amount_uzs} [{self.status}]"

    def save(self, *args, **kwargs) -> None:
        guard_immutable_scope_snapshot(
            self,
            field_attnames=(
                "branch_at_payment_id",
                "department_at_payment_id",
                "attribution_status",
            ),
            update_fields=kwargs.get("update_fields"),
        )
        super().save(*args, **kwargs)

    # --- Payme transaction-shape adapter (used by the Payme JSON-RPC store) ---
    @property
    def create_time_ms(self) -> int:
        return self.provider_created_at_ms or 0

    @property
    def perform_time_ms(self) -> int:
        return int(self.metadata.get("perform_time_ms", 0) or 0)

    @property
    def cancel_time_ms(self) -> int:
        return int(self.metadata.get("cancel_time_ms", 0) or 0)


class PaymentAttempt(models.Model):
    """One privacy-minimized provider round-trip outcome."""

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="attempts")
    attempt_no = models.PositiveSmallIntegerField()
    error_code = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("payment", "attempt_no")
        indexes = [models.Index(fields=("payment", "attempt_no"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"attempt#{self.attempt_no} of payment {self.payment_id}"


class WebhookEvent(models.Model):
    """Replay-protection ledger (D3-B-6). ``(provider, event_id)`` is unique — a
    replayed nonce is recorded as ``duplicate`` and side effects run zero times."""

    class Status(models.TextChoices):
        RECEIVED = "received", _("Received")
        PROCESSED = "processed", _("Processed")
        REJECTED = "rejected", _("Rejected")
        DUPLICATE = "duplicate", _("Duplicate")

    provider = models.CharField(max_length=16)
    event_id = models.CharField(max_length=128)  # provider txn id / Payme id / nonce
    signature_valid = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_attempted_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "event_id"), name="webhookevent_provider_event_unique"
            ),
        ]
        indexes = [models.Index(fields=("provider", "event_id"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.provider}:{self.event_id} [{self.status}]"


class FiscalReceipt(models.Model):
    """Soliq fiscal receipt for a completed payment (TD-7). One per payment."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        SUBMITTED = "submitted", _("Submitted")
        CONFIRMED = "confirmed", _("Confirmed")
        FAILED = "failed", _("Failed")

    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name="fiscal_receipt")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    fiscal_sign = models.CharField(max_length=128, blank=True)
    qr_url = models.URLField(blank=True)
    provider_payload = models.JSONField(default=dict, blank=True)
    pdf_key = models.CharField(max_length=512, blank=True)
    # Deprecated mixed-trust storage. Kept for a safe rolling migration only;
    # application code never reads download keys or provider data from it.
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    submitted_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover
        return f"receipt for payment {self.payment_id} [{self.status}]"
